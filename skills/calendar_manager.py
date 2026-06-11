"""LEGACY reminder skill — superseded by skills/commitment_manager.py (2026-06).

Open reminders were migrated into the commitments table on commitment_manager's first
init. Kept only so the legacy `reminders` table stays readable; not wired into the
agent, engine, or briefing anymore. Don't add features here.
"""
import os
import sqlite3
from datetime import datetime, timedelta
from langchain_core.tools import tool

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'aria_calendar.db')

def init_db():
    """Initializes the SQLite calendar database if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            target_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# Ensure the DB is initialized when this file is imported
init_db()

def get_todays_reminders() -> list:
    """Fetches all incomplete reminders for today (or older missed ones)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # We fetch anything scheduled for today, or anything scheduled previously that wasn't completed
    cursor.execute('''
        SELECT id, task, target_date FROM reminders 
        WHERE target_date <= ? AND completed = 0
        ORDER BY target_date ASC
    ''', (today_str,))
    
    results = cursor.fetchall()
    conn.close()
    
    return [{"id": r[0], "task": r[1], "date": r[2]} for r in results]

def mark_reminder_completed(reminder_id: int):
    """Marks a reminder as done (can be used by Aria later if she wants to check things off)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE reminders SET completed = 1 WHERE id = ?', (reminder_id,))
    conn.commit()
    conn.close()

@tool
def add_reminder(task: str, target_date_iso: str = None) -> str:
    """Use this tool when the user asks you to remind them about something or adds something to their agenda.
    
    Args:
        task: The description of the reminder or task (e.g. "Call Mom", "Dentist Appointment at 3 PM")
        target_date_iso: The target date in YYYY-MM-DD format. If the user says "tomorrow", calculate the exact date. If the user doesn't specify a day, set it to the current day.
    """
    if not target_date_iso:
        target_date_iso = datetime.now().strftime('%Y-%m-%d')
        
    try:
        # Validate date format
        datetime.strptime(target_date_iso, '%Y-%m-%d')
    except ValueError:
        return f"Error: target_date_iso must be in YYYY-MM-DD format. You provided {target_date_iso}."
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        INSERT INTO reminders (task, target_date, created_at, completed)
        VALUES (?, ?, ?, 0)
    ''', (task, target_date_iso, created_at))
    
    conn.commit()
    conn.close()
    
    return f"Successfully saved the reminder '{task}' for date {target_date_iso}!"
