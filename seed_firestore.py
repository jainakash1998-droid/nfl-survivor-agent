#!/usr/bin/env python3
"""Seed script for NFL Survivor Pool Strategist Firestore collections."""

from google.cloud import firestore

# CRITICAL: Always hardcode project ID as a string, never use google.auth.default() or GOOGLE_CLOUD_PROJECT
FIRESTORE_PROJECT_ID = "qwiklabs-gcp-03-812916682241"

def seed_database():
    print(f"Connecting to Firestore for project: {FIRESTORE_PROJECT_ID}")
    db = firestore.Client(project=FIRESTORE_PROJECT_ID)

    # 1. Seed survivor_games collection
    games = [
        {
            "game_id": "week4_kc_lac",
            "week": 4,
            "home_team": "Kansas City Chiefs",
            "away_team": "Los Angeles Chargers",
            "favorite": "Kansas City Chiefs",
            "spread": -7.5,
            "win_probability": 0.76,
            "game_time": "2026-10-04 16:25 EDT",
        },
        {
            "game_id": "week4_sf_ne",
            "week": 4,
            "home_team": "San Francisco 49ers",
            "away_team": "New England Patriots",
            "favorite": "San Francisco 49ers",
            "spread": -10.5,
            "win_probability": 0.84,
            "game_time": "2026-10-04 16:05 EDT",
        },
        {
            "game_id": "week4_buf_bal",
            "week": 4,
            "home_team": "Baltimore Ravens",
            "away_team": "Buffalo Bills",
            "favorite": "Baltimore Ravens",
            "spread": -2.5,
            "win_probability": 0.58,
            "game_time": "2026-10-04 20:20 EDT",
        },
        {
            "game_id": "week5_phi_cle",
            "week": 5,
            "home_team": "Philadelphia Eagles",
            "away_team": "Cleveland Browns",
            "favorite": "Philadelphia Eagles",
            "spread": -8.5,
            "win_probability": 0.79,
            "game_time": "2026-10-11 13:00 EDT",
        },
        {
            "game_id": "week5_det_dal",
            "week": 5,
            "home_team": "Dallas Cowboys",
            "away_team": "Detroit Lions",
            "favorite": "Detroit Lions",
            "spread": -3.0,
            "win_probability": 0.61,
            "game_time": "2026-10-11 16:25 EDT",
        },
    ]

    print("Seeding 'survivor_games' collection...")
    games_ref = db.collection("survivor_games")
    for game in games:
        games_ref.document(game["game_id"]).set(game)
        print(f"  ✓ Added game: {game['game_id']} ({game['away_team']} @ {game['home_team']})")

    # 2. Seed survivor_picks collection
    picks = [
        {
            "pick_id": "user_default_week1",
            "user_id": "user_default",
            "week": 1,
            "picked_team": "Buffalo Bills",
            "status": "WIN",
            "timestamp": "2026-09-10T20:00:00Z",
        },
        {
            "pick_id": "user_default_week2",
            "user_id": "user_default",
            "week": 2,
            "picked_team": "San Francisco 49ers",
            "status": "WIN",
            "timestamp": "2026-09-17T20:00:00Z",
        },
    ]

    print("Seeding 'survivor_picks' collection...")
    picks_ref = db.collection("survivor_picks")
    for pick in picks:
        picks_ref.document(pick["pick_id"]).set(pick)
        print(f"  ✓ Added pick: Week {pick['week']} -> {pick['picked_team']} ({pick['status']})")

    print("\n🎉 Firestore database successfully seeded!")

if __name__ == "__main__":
    seed_database()
