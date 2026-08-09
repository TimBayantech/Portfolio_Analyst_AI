from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ----- Users -----
class User(db.Model):
    __tablename__ = "users"

    __table_args__ = (
        db.UniqueConstraint("organization_id", "username", name="unique_org_username"),
        db.UniqueConstraint("organization_id", "email", name="unique_org_email"),
    )

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(
        db.String(50),
        nullable=False
    )

    password_hash = db.Column(
        db.String(200),
        nullable=False
    )

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False
    )

    role = db.Column(
        db.String(20),
        default="viewer"
    )

    created_at = db.Column(
        db.DateTime,
        default=db.func.now()
    )

    organization = db.relationship(
        "Organization",
        backref=db.backref("users", lazy=True)
    )

    email = db.Column(db.String(120), nullable=False)

# ----- Organization ------
class Organization(db.Model):
    __tablename__ = "organizations"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(
        db.String(100),
        nullable=False,
        unique=True
    )

    created_at = db.Column(
        db.DateTime,
        default=db.func.now()
    )

# ----- Portfolio -----
class Portfolio(db.Model):
    __tablename__ = "portfolios"

    id = db.Column(db.Integer, primary_key=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False
    )

    month = db.Column(db.String(7), nullable=False)

    start_date = db.Column(db.Date, nullable=False)

    created_at = db.Column(
        db.DateTime,
        default=db.func.now()
    )

    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "month",
            name="unique_user_month"
        ),
    )

# ----- Portfolio Tickers -----
class PortfolioTicker(db.Model):
    __tablename__ = "portfolio_tickers"

    id = db.Column(db.Integer, primary_key=True)

    ticker = db.Column(
        db.String(10),
        nullable=False
    )

    market_index = db.Column(
        db.String(20)
    )

    buy_price = db.Column(
        db.Float
    )  # optional, store actual buy price

    portfolio_id = db.Column(
        db.Integer,
        db.ForeignKey("portfolios.id"),
        nullable=False
    )

    portfolio = db.relationship(
        "Portfolio",
        backref=db.backref("tickers", lazy=True)
    )

    date_bought = db.Column(
        db.Date,
        nullable=False
    )

    date_sold = db.Column(
        db.Date,
        nullable=True
    )  # None until sold

# ----- Portfolio Settings -----
class PortfolioSettings(db.Model):
    __tablename__ = "portfolio_settings"

    id = db.Column(db.Integer, primary_key=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False,
        unique=True
    )

    tp1 = db.Column(db.Float)
    tp2 = db.Column(db.Float)
    tp3 = db.Column(db.Float)
    stop_loss = db.Column(db.Float)

    tp1_hit = db.Column(db.Boolean, default=False)
    tp2_hit = db.Column(db.Boolean, default=False)
    tp3_hit = db.Column(db.Boolean, default=False)
    sl_hit = db.Column(db.Boolean, default=False)

    updated_at = db.Column(
        db.DateTime,
        default=db.func.now(),
        onupdate=db.func.now()
    )

# ----- Alert Emails -----
class AlertEmail(db.Model):
    __tablename__ = "alert_emails"

    id = db.Column(db.Integer, primary_key=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False
    )

    email = db.Column(
        db.String(120),
        nullable=False
    )

# ----- Ticker Alert State -----
class AlertState(db.Model):
    __tablename__ = "alert_state"

    id = db.Column(db.Integer, primary_key=True)

    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organizations.id"),
        nullable=False
    )

    ticker = db.Column(
        db.String(10),
        nullable=False
    )

    last_alert_time = db.Column(
        db.DateTime,
        nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "ticker",
            name="unique_org_ticker_alert"
        ),
    )