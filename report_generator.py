"""Renders the daily Markdown briefing consumed by main.py.

Takes email classifications, raw emails, the news brief, and today's reminders;
writes reports/daily_summary_<date>.md and returns (path, markdown_string).
"""
import os
from datetime import datetime

REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")

def generate_daily_markdown(classifications, emails_data, news_briefing=None, reminders=None):
    """Takes LLM classifications, raw email data, news, and reminders to build a central markdown report."""
    if not os.path.exists(REPORT_DIR):
        os.makedirs(REPORT_DIR)
        
    date_str = datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(REPORT_DIR, f"daily_summary_{date_str}.md")
    
    important = []
    news = []
    deleted = []
    
    for item in classifications:
        email_info = next((e for e in emails_data if e['id'] == item['id']), None)
        if not email_info: continue
            
        if item['action'] == 'DELETE':
            deleted.append(email_info['subject'])
        elif item['category'] == 'IMPORTANT':
            important.append((email_info, item))
        else:
            news.append((email_info, item))
            
    with open(report_path, "w") as f:
        f.write(f"# Daily Assistant Summary - {date_str}\n\n")
        
        if reminders:
            f.write("## 📌 Commitments & Agenda\n")
            for r in reminders:
                f.write(f"- [ ] {r['task']}\n")
            f.write("\n")
            
        if news_briefing:
            f.write("## 🌍 Your Daily News Briefing\n")
            for topic in news_briefing:
                f.write(f"### {topic.get('headline', 'News Topic')}\n")
                f.write(f"*{', '.join(topic.get('sources', []))}*\n")
                f.write(f"{topic.get('summary', '')}\n\n")
                
        f.write("## 🚨 Action Required / Important Emails\n")
        if not important:
            f.write("*None today.*\n")
        for em, clf in important:
            f.write(f"- **{em['sender']}**: {em['subject']}\n")
            f.write(f"  - *{clf['summary']}*\n")
            
        f.write("\n## 📰 News & Updates\n")
        if not news:
            f.write("*None today.*\n")
        for em, clf in news:
            f.write(f"- **{em['sender']}**: {em['subject']}\n")
            f.write(f"  - *{clf['summary']}*\n")
            
        f.write("\n## 🗑️ Cleaned Up (Trash/Promos)\n")
        f.write(f"*{len(deleted)} emails were labeled for deletion:*\n")
        for subject in deleted:
            f.write(f"- {subject}\n")
            
    print(f"\n✅ Daily summary written to {report_path}")
    
    with open(report_path, "r") as f:
        markdown_content = f.read()
        
    return report_path, markdown_content
