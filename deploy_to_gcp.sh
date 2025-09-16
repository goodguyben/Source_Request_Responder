#!/bin/bash
# GCP Deployment Script using gcloud CLI
# This script uploads your project to GCP and creates a VM instance

set -e  # Exit on any error

echo "🚀 GCP Deployment Script for Source Request Responder"
echo "=================================================="

# Configuration
PROJECT_ID=""
BUCKET_NAME=""
ZONE="us-central1-a"
INSTANCE_NAME="source-responder-vm"
MACHINE_TYPE="e2-micro"

# Get user input
read -p "Enter your GCP Project ID: " PROJECT_ID
read -p "Enter bucket name (will be created if doesn't exist): " BUCKET_NAME
read -p "Enter zone (default: us-central1-a): " ZONE_INPUT
if [ ! -z "$ZONE_INPUT" ]; then
    ZONE="$ZONE_INPUT"
fi

echo "📋 Configuration:"
echo "  Project ID: $PROJECT_ID"
echo "  Bucket: $BUCKET_NAME"
echo "  Zone: $ZONE"
echo "  Instance: $INSTANCE_NAME"
echo ""

# Set the project
echo "🔧 Setting GCP project..."
gcloud config set project $PROJECT_ID

# Enable required APIs
echo "🔧 Enabling required APIs..."
gcloud services enable compute.googleapis.com
gcloud services enable storage.googleapis.com

# Create bucket if it doesn't exist
echo "🪣 Creating/checking bucket..."
gsutil mb -p $PROJECT_ID gs://$BUCKET_NAME 2>/dev/null || echo "Bucket already exists"

# Create deployment zip
echo "📦 Creating deployment package..."
ZIP_FILE="source-responder-deploy.zip"

# Remove existing zip if it exists
rm -f $ZIP_FILE

# Create zip excluding unnecessary files
zip -r $ZIP_FILE . \
    -x "venv/*" \
    -x "__pycache__/*" \
    -x "*.pyc" \
    -x ".git/*" \
    -x "*.log" \
    -x "data/app.db" \
    -x "logs/*" \
    -x "nohup.log" \
    -x "upload_to_gcp.py" \
    -x "deploy_to_gcp.sh" \
    -x ".DS_Store"

echo "✅ Created deployment package: $ZIP_FILE"

# Upload to GCS
echo "☁️ Uploading to Google Cloud Storage..."
gsutil cp $ZIP_FILE gs://$BUCKET_NAME/deployments/

# Create startup script
echo "📝 Creating startup script..."
cat > startup-script.sh << 'EOF'
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
EOF

# Startup script is ready (bucket name already set)

# Check if instance already exists
echo "🔍 Checking if VM instance exists..."
if gcloud compute instances describe $INSTANCE_NAME --zone=$ZONE --project=$PROJECT_ID >/dev/null 2>&1; then
    echo "⚠️  VM instance already exists: $INSTANCE_NAME"
    read -p "Do you want to delete and recreate it? (y/N): " RECREATE
    if [[ $RECREATE =~ ^[Yy]$ ]]; then
        echo "🗑️  Deleting existing instance..."
        gcloud compute instances delete $INSTANCE_NAME --zone=$ZONE --project=$PROJECT_ID --quiet
    else
        echo "✅ Using existing instance"
        INSTANCE_EXISTS=true
    fi
fi

# Create VM instance if it doesn't exist
if [ "$INSTANCE_EXISTS" != "true" ]; then
    echo "🖥️  Creating VM instance..."
    gcloud compute instances create $INSTANCE_NAME \
        --zone=$ZONE \
        --machine-type=$MACHINE_TYPE \
        --image-family=ubuntu-2204-lts \
        --image-project=ubuntu-os-cloud \
        --boot-disk-size=10GB \
        --boot-disk-type=pd-standard \
        --metadata-from-file startup-script=startup-script.sh \
        --tags=http-server,https-server \
        --project=$PROJECT_ID
    
    echo "⏳ VM instance created. Startup script is running..."
    echo "⏳ This may take 5-10 minutes to complete..."
fi

# Get VM external IP
echo "🌐 Getting VM external IP..."
EXTERNAL_IP=$(gcloud compute instances describe $INSTANCE_NAME --zone=$ZONE --project=$PROJECT_ID --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

if [ ! -z "$EXTERNAL_IP" ]; then
    echo "✅ VM External IP: $EXTERNAL_IP"
else
    echo "⚠️  Could not get external IP"
fi

# Cleanup
echo "🧹 Cleaning up..."
rm -f $ZIP_FILE startup-script.sh

echo ""
echo "🎉 Deployment completed!"
echo "=========================="
echo "📊 VM Details:"
echo "  Name: $INSTANCE_NAME"
echo "  Zone: $ZONE"
echo "  External IP: $EXTERNAL_IP"
echo ""
echo "🔗 Useful Commands:"
echo "  SSH into VM: gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo "  View logs: gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo journalctl -u source-responder -f'"
echo "  Restart service: gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo systemctl restart source-responder'"
echo ""
echo "📝 Next Steps:"
echo "1. SSH into your VM: gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo "2. Upload your credentials.json and token.json files"
echo "3. Create .env file with your API keys"
echo "4. Start the service: sudo systemctl start source-responder"
echo "5. Monitor logs: sudo journalctl -u source-responder -f"
echo ""
echo "📊 Monitor your VM at: https://console.cloud.google.com/compute/instances"
