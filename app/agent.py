# ruff: noqa
import base64
import datetime
import json
import os
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types
import requests

from .a2ui_utils import a2ui_callback

MODEL = "gemini-3.8-flash"

# IMPORTANT: Hardcoded GCP project ID, GCS bucket name, & Memory Bank ID as strings.
# Do NOT read from google.auth.default() or GOOGLE_CLOUD_PROJECT because on Agent Platform those return project number.
FIRESTORE_PROJECT_ID = "qwiklabs-gcp-03-812916682241"
BUCKET_NAME = "nfl-survivor-assets-812916682241"
MEMORY_BANK_ID = "6164713207312154624"

# Load Agent Engine resource name from deployment_metadata.json if available
DEPLOYMENT_METADATA_PATH = (
    Path(__file__).parent.parent / "deployment_metadata.json"
)
agent_engine_resource_name = None
if DEPLOYMENT_METADATA_PATH.exists():
    try:
        with open(DEPLOYMENT_METADATA_PATH, "r") as f:
            metadata = json.load(f)
            agent_engine_resource_name = metadata.get("remote_agent_runtime_id")
    except Exception:
        pass

if not agent_engine_resource_name:
    agent_engine_resource_name = f"projects/{FIRESTORE_PROJECT_ID}/locations/us-east1/reasoningEngines/{MEMORY_BANK_ID}"

code_executor = AgentEngineSandboxCodeExecutor(
    agent_engine_resource_name=agent_engine_resource_name
)

db = firestore.Client(project=FIRESTORE_PROJECT_ID)
storage_client = storage.Client(project=FIRESTORE_PROJECT_ID)
genai_client = genai.Client(
    vertexai=True, project=FIRESTORE_PROJECT_ID, location="global"
)


# WRITE: Memory callback to extract durable facts and preferences into Memory Bank after each turn
async def generate_memories_callback(callback_context: CallbackContext):
    await callback_context.add_session_to_memory()
    return None


# Memory service builder for deployment wiring
def memory_bank_service_builder():
    return VertexAiMemoryBankService(
        project=FIRESTORE_PROJECT_ID,
        location="us-east1",
        agent_engine_id=MEMORY_BANK_ID,
    )


def get_weekly_schedule_and_odds(week: int) -> str:
    """Reads NFL games schedule, spreads, and win probabilities for a specific week from Firestore.

    Args:
        week: The NFL week number (e.g. 4, 5).

    Returns:
        A formatted string listing the games, point spreads, favorites, and win probabilities for that week.
    """
    try:
        games_ref = db.collection("survivor_games")
        query = games_ref.where("week", "==", int(week)).stream()
        results = []
        for doc in query:
            data = doc.to_dict()
            results.append(
                f"- {data.get('away_team')} @ {data.get('home_team')} | Favorite: {data.get('favorite')} ({data.get('spread')}) | Win Prob: {int(data.get('win_probability', 0) * 100)}% | Time: {data.get('game_time')}"
            )

        if not results:
            return f"No games found in Firestore for Week {week}."

        return f"### NFL Week {week} Schedule & Odds:\n" + "\n".join(results)
    except Exception as e:
        return f"Error reading schedule from Firestore: {e}"


def get_used_teams(user_id: str = "user_default") -> str:
    """Reads previously picked Survivor teams for a user from Firestore to see which teams are unavailable.

    Args:
        user_id: The ID of the user (defaults to 'user_default').

    Returns:
        A summary of previously picked teams and weeks.
    """
    try:
        picks_ref = db.collection("survivor_picks")
        query = picks_ref.where("user_id", "==", user_id).stream()
        used = []
        for doc in query:
            data = doc.to_dict()
            used.append(
                f"Week {data.get('week')}: {data.get('picked_team')} ({data.get('status', 'PENDING')})"
            )

        if not used:
            return f"No previous picks found for user '{user_id}'. All teams are available!"

        return (
            f"### Used Teams for User '{user_id}':\n"
            + "\n".join(used)
            + "\n\n⚠️ Note: You CANNOT pick these teams again for the rest of the season."
        )
    except Exception as e:
        return f"Error reading user picks from Firestore: {e}"


def record_survivor_pick(
    picked_team: str, week: int, user_id: str = "user_default"
) -> str:
    """Records a new Survivor Pool pick for a user in Firestore after checking if the team was already used.

    Args:
        picked_team: The name of the NFL team being picked (e.g. 'Kansas City Chiefs').
        week: The NFL week number for the pick (e.g. 4).
        user_id: The ID of the user making the pick (defaults to 'user_default').

    Returns:
        Confirmation message if successful, or an error/warning if team was already used.
    """
    try:
        picks_ref = db.collection("survivor_picks")
        # Check if team already used in prior weeks
        existing_picks = (
            picks_ref.where("user_id", "==", user_id).limit(50).stream()
        )
        for doc in existing_picks:
            data = doc.to_dict()
            if (
                data.get("picked_team", "").strip().lower()
                == picked_team.strip().lower()
            ):
                return (
                    f"❌ REJECTED: You already picked the {picked_team} in Week {data.get('week')}! "
                    "In Survivor pools, you can only pick each team once per season."
                )

        pick_id = f"{user_id}_week{week}"
        doc_ref = picks_ref.document(pick_id)
        pick_data = {
            "pick_id": pick_id,
            "user_id": user_id,
            "week": int(week),
            "picked_team": picked_team,
            "status": "PENDING",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        doc_ref.set(pick_data)
        return f"✅ SUCCESS: Recorded pick for Week {week}: **{picked_team}** for user '{user_id}'."
    except Exception as e:
        return f"Error recording pick in Firestore: {e}"


def simulate_survivor_path(
    user_id: str = "user_default", forecast_weeks: int = 2
) -> str:
    """Calculates the optimal multi-week survivor pick path and cumulative survival probability based on unused teams and odds.

    Args:
        user_id: The ID of the user (defaults to 'user_default').
        forecast_weeks: The number of future weeks to optimize (defaults to 2).

    Returns:
        A strategic recommendation showing optimal pick paths and overall expected survival probability.
    """
    try:
        picks_ref = db.collection("survivor_picks")
        user_picks = picks_ref.where("user_id", "==", user_id).stream()
        used_teams = set()
        max_used_week = 3
        for p in user_picks:
            data = p.to_dict()
            team = data.get("picked_team", "").strip().lower()
            if team:
                used_teams.add(team)
            week = data.get("week", 0)
            if week > max_used_week:
                max_used_week = week

        start_week = max_used_week + 1
        path = []
        cum_prob = 1.0

        for w in range(start_week, start_week + forecast_weeks):
            games = (
                db.collection("survivor_games")
                .where("week", "==", w)
                .stream()
            )
            best_pick = None
            best_prob = -1.0
            best_game_str = ""

            for doc in games:
                g = doc.to_dict()
                fav = g.get("favorite", "")
                prob = float(g.get("win_probability", 0.5))

                if fav.strip().lower() not in used_teams and prob > best_prob:
                    best_prob = prob
                    best_pick = fav
                    best_game_str = f"Week {w}: Pick **{fav}** ({g.get('spread')}) — Win Prob: {int(prob * 100)}%"

            if best_pick:
                used_teams.add(best_pick.strip().lower())
                cum_prob *= best_prob
                path.append(best_game_str)
            else:
                path.append(f"Week {w}: No unused favorite available in schedule data.")

        if not path:
            return "No upcoming weeks found to simulate."

        overall_pct = int(cum_prob * 100)
        return (
            f"### 🎯 Optimal {forecast_weeks}-Week Survivor Path (Starting Week {start_week}):\n\n"
            + "\n".join(path)
            + f"\n\n📊 **Cumulative {forecast_weeks}-Week Survival Probability**: **{overall_pct}%**"
        )
    except Exception as e:
        return f"Error simulating survivor path: {e}"


def fetch_real_nfl_scoreboard() -> str:
    """Fetches real-time live NFL scoreboard data, scores, and status directly from ESPN's public sports API.

    Returns:
        A summary of live real NFL game matchups, current scores, and game status.
    """
    try:
        api_url = os.getenv(
            "NFL_SCOREBOARD_API_URL",
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        )
        api_key = os.getenv("SPORTS_API_KEY", None)

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        response = requests.get(api_url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        events = data.get("events", [])
        if not events:
            return "No real-time NFL events currently found."

        results = []
        for event in events[:6]:
            status = event.get("status", {}).get("type", {}).get("detail", "")
            competitions = event.get("competitions", [{}])[0]
            competitors = competitions.get("competitors", [])

            lines = []
            for comp in competitors:
                team_name = comp.get("team", {}).get("displayName", "")
                score = comp.get("score", "0")
                lines.append(f"{team_name} ({score})")

            matchup = " vs ".join(lines) if lines else event.get("name", "NFL Game")
            results.append(f"- **{matchup}** | *{status}*")

        return (
            "### 🏈 Live Real-Time NFL Scoreboard (Public ESPN API):\n"
            + "\n".join(results)
        )
    except Exception as e:
        return f"Error fetching real NFL scores: {e}"


async def generate_survivor_badge(
    team_name: str, week: int, tool_context: ToolContext
) -> str:
    """Generates a custom NFL Survivor Shield badge image using gemini-3.1-flash-lite-image in the global region. Saves the image as an artifact and uploads it to Cloud Storage.

    Args:
        team_name: The name of the NFL team (e.g. 'Kansas City Chiefs').
        week: The NFL week number (e.g. 4).
        tool_context: The ADK ToolContext injected by the framework.

    Returns:
        The public HTTPS URL of the uploaded image in Cloud Storage.
    """
    try:
        prompt = (
            f"A dramatic 3D metallic tactical NFL Survivor Shield badge logo for the {team_name}, "
            f"celebrating Week {week} Survivor victory, high resolution glossy emblem."
        )
        res = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
        )

        part = res.candidates[0].content.parts[0]
        image_bytes = part.inline_data.data
        mime_type = part.inline_data.mime_type or "image/png"

        ext = "png" if "png" in mime_type else "jpg"
        clean_team = (
            team_name.lower().replace(" ", "_").replace("'", "").replace("/", "")
        )
        filename = f"survivor_badge_{clean_team}_week{week}.{ext}"

        # 1. Save artifact for Playground Artifacts panel
        artifact_part = types.Part.from_bytes(
            data=image_bytes, mime_type=mime_type
        )
        await tool_context.save_artifact(
            filename=filename,
            artifact=artifact_part,
        )

        # 2. Upload exact image bytes to public Cloud Storage bucket
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
        return (
            f"Successfully generated Survivor Shield badge for {team_name} (Week {week})!\n\n"
            f"Public Image URL: {public_url}"
        )
    except Exception as e:
        return f"Error generating survivor badge image: {e}"


async def generate_survivor_video(
    team_name: str, topic: str, tool_context: ToolContext
) -> str:
    """Generates a short NFL Survivor video using Google's Omni model (gemini-omni-flash-preview) in the global region.
    Saves the generated video as an artifact and uploads the video bytes to Cloud Storage.

    Args:
        team_name: The name of the NFL team (e.g. 'Kansas City Chiefs').
        topic: Brief description of the video theme (e.g. 'touchdown celebration', '3D shield animation').
        tool_context: The ADK ToolContext injected by the framework.

    Returns:
        The public HTTPS URL of the uploaded video in Cloud Storage.
    """
    try:
        prompt = (
            f"Generate a short video for the {team_name}: {topic}, "
            "3D metallic NFL stadium celebration animation, broadcast quality."
        )
        res = genai_client.interactions.create(
            model="gemini-omni-flash-preview",
            input=prompt,
            response_modalities=["video"],
        )

        video_content = getattr(res, "output_video", None)
        if not video_content:
            return "Error: No video content returned from gemini-omni-flash-preview interaction."

        video_bytes = None
        if getattr(video_content, "data", None):
            raw_data = video_content.data
            if isinstance(raw_data, str):
                video_bytes = base64.b64decode(raw_data)
            elif isinstance(raw_data, bytes):
                video_bytes = raw_data
        elif getattr(video_content, "uri", None):
            uri = video_content.uri
            if uri.startswith("gs://"):
                parts = uri[5:].split("/", 1)
                b_name = parts[0]
                b_path = parts[1] if len(parts) > 1 else ""
                blob = storage_client.bucket(b_name).blob(b_path)
                video_bytes = blob.download_as_bytes()
            else:
                import httpx
                resp = httpx.get(uri)
                video_bytes = resp.content

        if not video_bytes:
            return "Error: Could not extract video bytes from gemini-omni-flash-preview output."

        mime_type = "video/mp4"
        clean_team = (
            team_name.lower().replace(" ", "_").replace("'", "").replace("/", "")
        )
        filename = f"survivor_video_{clean_team}_{uuid.uuid4().hex[:6]}.mp4"

        # 1. Save artifact for Playground Artifacts panel
        artifact_part = types.Part.from_bytes(
            data=video_bytes, mime_type=mime_type
        )
        await tool_context.save_artifact(
            filename=filename,
            artifact=artifact_part,
        )

        # 2. Upload exact video bytes to public Cloud Storage bucket
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
        return (
            f"Successfully generated Survivor video for {team_name}!\n\n"
            f"Public Video URL: {public_url}"
        )
    except Exception as e:
        return f"Error generating survivor video: {e}"


# Build A2UI system prompt using A2uiSchemaManager version 0.8 and BasicCatalog
schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are the NFL Survivor Pool Strategist agent. "
        "You help users make optimal weekly Survivor Pool picks by checking game schedules, point spreads, "
        "win probabilities, and tracking previously used teams so they never make invalid picks. "
        "You remember user preferences, favorite teams, and past strategy requests across sessions. "
        "You can simulate multi-week optimal survival paths using expected probability calculations or Python code sandbox execution, "
        "fetch real-time live NFL scores and game results using ESPN's public API, "
        "and generate custom NFL Survivor victory badges and videos."
    ),
    workflow_description="Analyze the request, call relevant tools if needed, and return structured UI components when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)

root_agent = Agent(
    name="simple_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=instruction,
    tools=[
        PreloadMemoryTool(),
        get_weekly_schedule_and_odds,
        get_used_teams,
        record_survivor_pick,
        simulate_survivor_path,
        fetch_real_nfl_scoreboard,
        generate_survivor_badge,
        generate_survivor_video,
    ],
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
    code_executor=code_executor,
)

app = App(
    root_agent=root_agent,
    name="app",
)
