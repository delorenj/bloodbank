#!/bin/bash
cd ~/code/33GOD/bloodbank
# Use the venv Python directly so PM2 can manage the process tree
exec /home/delorenj/code/33GOD/bloodbank/.venv/bin/python -m uvicorn event_producers.http:app --host 0.0.0.0 --port 8682
