import os
from dotenv import load_dotenv
from app import create_app

load_dotenv()

# Enable scheduler for local development (checks every 5 seconds)
# Set SCHEDULER_ENABLED=1 in .env to activate
start_scheduler = os.environ.get('SCHEDULER_ENABLED', '0') == '1'

app = create_app(start_scheduler=start_scheduler)

if __name__ == '__main__':
    app.run()
