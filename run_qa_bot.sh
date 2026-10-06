#!/bin/bash
# Load environment variables
set -a
source /Users/adammarq/CascadeProjects/openclaw-inbox-agent/.env
set +a

cd /Users/adammarq/CascadeProjects/openclaw-inbox-agent
exec /usr/bin/python3 -u scoring_qa_bot.py
