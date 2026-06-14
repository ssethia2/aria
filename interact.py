"""Interactive REPL (local interface).

A thin terminal front-end over the shared agent in agent_core.py. Run:
`python3 interact.py`. Conversation is checkpointed to SQLite under a fixed thread id,
so it survives restarts — type 'new' to start a fresh conversation thread.
For the phone-reachable interface see telegram_bot.py; for the unattended morning
email job see main.py.
"""
import uuid

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from core.agent_core import build_agent, open_checkpointer, thread_config, extract_text

load_dotenv()


def interact_loop():
    print("Initializing Aria's Neural Pathways...")

    try:
        agent = build_agent(checkpointer=open_checkpointer())
    except Exception as e:
        print(f"Failed to initialize LLM: {e}")
        return

    thread_id = "local-repl"

    print("\n" + "=" * 50)
    print("🎙️  Aria is online. Type 'exit' to quit, 'new' for a fresh conversation.")
    print("=" * 50)

    while True:
        try:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue

            if user_input.lower() in ['exit', 'quit']:
                print("\nAria: Goodbye! Shutting down systems.")
                break

            if user_input.lower() == 'new':
                thread_id = f"local-repl-{uuid.uuid4().hex[:8]}"
                print("\nAria: Fresh conversation started.")
                continue

            result = agent.invoke({"messages": [HumanMessage(content=user_input)]},
                                  config=thread_config(thread_id))

            print(f"\nAria: {extract_text(result['messages'][-1].content)}")

        except KeyboardInterrupt:
            print("\nAria: Goodbye! Shutting down systems.")
            break
        except Exception as e:
            print(f"\n[System Error]: {e}")


if __name__ == "__main__":
    interact_loop()
