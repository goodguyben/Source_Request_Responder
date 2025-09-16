#!/bin/bash
# Startup script for Source Request Responder

# Update system
apt-get update && apt-get upgrade -y

# Install dependencies
apt-get install -y python3 python3-pip python3-venv git curl wget unzip

# Create project directory
mkdir -p /opt/source-responder
cd /opt/source-responder

# Download and extract project
gsutil cp gs://BUCKET_NAME/deployments/source-responder-deploy.zip .
unzip source-responder-deploy.zip

# Set up Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create directories
mkdir -p data logs

# Create systemd service
cat > /etc/systemd/system/source-responder.service << 'SERVICE_EOF'
[Unit]
Description=Source Request Responder Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/source-responder
ExecStart=/opt/source-responder/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PATH=/opt/source-responder/venv/bin

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Enable and start service
systemctl daemon-reload
systemctl enable source-responder

echo "Deployment completed successfully!"
echo "Remember to:"
echo "1. Upload your credentials.json and token.json files"
echo "2. Set up your .env file with API keys"
echo "3. Start the service with: systemctl start source-responder"
