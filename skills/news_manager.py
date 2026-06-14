"""News skill — daily newsletter briefing.

Fetches the last day of Morning Brew / NYT / CNN newsletters, scrapes their full
bodies, and uses the LLM router to aggregate them into deduped topics (flagging
Morning Brew exclusives). Entry point: generate_news_brief().
"""
import os
import sys
import base64
import json
from bs4 import BeautifulSoup

from langchain_core.prompts import PromptTemplate

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skills.email_manager import get_gmail_service

def extract_body(payload):
    """Recursively extract the plain text or HTML body from the email payload."""
    if 'data' in payload.get('body', {}):
        data = payload['body']['data']
        # Google returns base64url encoded strings
        decoded_data = base64.urlsafe_b64decode(data).decode('utf-8')
        mime_type = payload.get('mimeType')
        if mime_type == 'text/html':
            soup = BeautifulSoup(decoded_data, 'html.parser')
            return soup.get_text(separator=' ', strip=True)
        return decoded_data
        
    parts = payload.get('parts', [])
    text_content = ""
    for part in parts:
        mimeType = part.get('mimeType')
        if mimeType == 'text/plain':
            data = part['body'].get('data')
            if data:
                text_content += base64.urlsafe_b64decode(data).decode('utf-8') + "\n"
        elif mimeType == 'text/html':
            data = part['body'].get('data')
            if data:
                html = base64.urlsafe_b64decode(data).decode('utf-8')
                soup = BeautifulSoup(html, 'html.parser')
                text_content += soup.get_text(separator=' ', strip=True) + "\n"
        elif 'parts' in part:
             text_content += extract_body(part)
             
    return text_content

def fetch_daily_newsletters(service):
    """Fetch newsletters from the last 24 hours."""
    print("Fetching today's newsletters (Morning Brew, NYT, CNN)...")
    # You can customize these senders as needed
    query = "newer_than:1d AND (from:morningbrew OR from:nytimes OR from:cnn)"
    try:
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])

        if not messages:
            print("No new newsletters found today.")
            return []
        
        newsletters = []
        for message in messages:
            msg = service.users().messages().get(userId='me', id=message['id'], format='full').execute()
            
            headers = msg['payload'].get('headers', [])
            subject = next((header['value'] for header in headers if header['name'] == 'Subject'), 'No Subject')
            sender = next((header['value'] for header in headers if header['name'] == 'From'), 'Unknown Sender')
            
            # Determine source
            source = "Unknown"
            if "morningbrew" in sender.lower():
                source = "Morning Brew"
            elif "nytimes" in sender.lower():
                source = "NYT"
            elif "cnn" in sender.lower():
                source = "CNN"
            
            body = extract_body(msg['payload'])
            
            # Truncate aggressively — synthesis needs the substance, not the full
            # newsletter. ~6k chars captures the headlines/lede of each; big token cut.
            max_chars = 6000
            if len(body) > max_chars:
                body = body[:max_chars] + "...[TRUNCATED]"
                
            newsletters.append({
                'id': message['id'],
                'source': source,
                'subject': subject,
                'content': body
            })
            
        return newsletters

    except Exception as error:
        print(f'An error occurred fetching newsletters: {error}')
        return []

def generate_news_brief():
    """Generates a brief summary of today's newsletters using the LLM Fallback Router."""
    print("Running Skill: News Briefing...")
    
    # Assuming fetch_recent_newsletters is a new or renamed function that handles service initialization internally
    # For this change, we'll call the existing fetch_daily_newsletters, but the instruction implies a different function name.
    # To make the code syntactically correct and runnable with existing functions, we'll use fetch_daily_newsletters.
    # If fetch_recent_newsletters is intended to be a new function, it needs to be defined.
    # For now, we'll adapt to use the existing fetch_daily_newsletters by getting the service.
    service = get_gmail_service()
    if not service:
        print("Failed to start Gmail service.")
        return None
    
    newsletters = fetch_daily_newsletters(service) # Changed from fetch_recent_newsletters to use existing function
    if not newsletters:
        print("No newsletters to summarize today.")
        return None
        
    print("Synthesizing news with AI Router (Opus -> Sonnet -> Gemini)... (This may take a moment)")
    from core.llm_router import get_llm
    try:
        llm = get_llm(temperature=0)
    except Exception as e:
        print(f"Failed to intialize LLM. Error: {e}")
        return None

    prompt = PromptTemplate(
        input_variables=["newsletter_data"],
        template='''You are an expert news editor. Your user reads several daily newsletters. 
        I am giving you the raw, scraped text of today's newsletters. 
        
        Your task is to synthesize this information into a single, cohesive daily news brief.
        
        CRITICAL INSTRUCTIONS:
        1. Combine articles from different sources (e.g. NYT and CNN) into a single topic if they are about the SAME news event. Do not repeat the same news twice.
        2. Give each topic a clear, engaging headline.
        3. Write a 2-3 sentence summary for each topic.
        4. If a topic was covered EXCLUSIVELY by Morning Brew, you must add an indicator like "[Morning Brew Exclusive]" to the headline.
        
        Newsletters Text:
        {newsletter_data}
        
        Return ONLY a raw JSON array of objects representing the summarized topics. Use this exact format:
        [
          {{
            "headline": "Topic Headline (add [Morning Brew Exclusive] if applicable)",
            "summary": "Brief 2-3 sentence summary of the aggregated topic.",
            "sources": ["NYT", "CNN"] // List the sources that contributed to this topic
          }}
        ]
        
        Return ONLY the raw JSON array. DO NOT use markdown code blocks.
        '''
    )

    # Format the input for the LLM
    formatted_data = ""
    for nl in newsletters:
        formatted_data += f"\\n--- SOURCE: {nl['source']} | SUBJECT: {nl['subject']} ---\\n"
        formatted_data += nl['content'] + "\\n"

    chain = prompt | llm
    
    print("Synthesizing news with AI... (This may take a moment)")
    response = chain.invoke({"newsletter_data": formatted_data})
    
    try:
        cleaned_response = response.content.strip().strip('```json').strip('```')
        brief_topics = json.loads(cleaned_response)
        print(f"✅ Generated brief with {len(brief_topics)} topics.")
        return brief_topics
    except json.JSONDecodeError:
        print("Failed to parse LLM news brief response. Raw output:")
        print(response.content)
        return None
