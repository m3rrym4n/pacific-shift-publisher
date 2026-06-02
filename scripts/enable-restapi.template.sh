#!/bin/sh

echo "Applying Castopod REST API configuration..."

chmod +w /app/.env

cat >> /app/.env << 'EOF'

restapi.enabled=true
restapi.basicAuth=true
restapi.basicAuthUsername="automation"
restapi.basicAuthPassword="changeme"
EOF

chmod -w /app/.env

echo "REST API configuration applied."
