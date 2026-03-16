#!/bin/bash
# setup_gcp.sh
# Run this script on your new GCP Compute Engine instance to initialize the assistant

echo "Updating system packages..."
sudo apt update && sudo apt upgrade -y

echo "Installing Python and dependencies..."
sudo apt install python3 python3-venv python3-pip git -y

echo "Setting up Virtual Environment..."
python3 -m venv venv
source venv/bin/activate

if [ -f "requirements.txt" ]; then
    echo "Installing requirements..."
    pip install -r requirements.txt
else
    echo "requirements.txt not found. Are you in the right directory?"
fi

echo "========================================================="
echo "Setup Complete! Here are your next steps:"
echo "1. Ensure you have transferred: .env, credentials.json, and profile.json to this directory."
echo "2. Run 'source venv/bin/activate && python3 main.py' manually one time to trigger the Gmail OAuth flow."
echo "3. Once token.json is generated, run 'crontab -e' and add this line:"
echo "   0 8 * * * $(pwd)/run.sh"
echo "========================================================="
