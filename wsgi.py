from dotenv import load_dotenv
from app import create_app

load_dotenv()

app = create_app(start_scheduler=False)

if __name__ == '__main__':
    app.run()
