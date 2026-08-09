from app import app, db
from models import Portfolio

with app.app_context():
    # 1️⃣ Create the new Portfolio table
    Portfolio.__table__.create(db.engine, checkfirst=True)
    print("✅ 'portfolios' table created (if not exists)")

    # 2️⃣ Add 'portfolio_id' column to portfolio_tickers
    try:
        db.engine.execute("ALTER TABLE portfolio_tickers ADD COLUMN portfolio_id INTEGER;")
        print("✅ 'portfolio_id' column added to portfolio_tickers")
    except Exception as e:
        print("⚠ 'portfolio_id' may already exist or error:", e)

    # 3️⃣ Add 'buy_price' column to portfolio_tickers
    try:
        db.engine.execute("ALTER TABLE portfolio_tickers ADD COLUMN buy_price FLOAT;")
        print("✅ 'buy_price' column added to portfolio_tickers")
    except Exception as e:
        print("⚠ 'buy_price' may already exist or error:", e)