from flask import Flask, render_template, request, session, redirect, flash, abort, jsonify, url_for
from flask_caching import Cache
from flask_migrate import Migrate
import pandas as pd
import yfinance as yf
import plotly.graph_objs as go
import plotly.io as pio
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
import os
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from sqlalchemy.exc import IntegrityError
import threading
from models import db, User, PortfolioTicker, PortfolioSettings, AlertEmail, Portfolio, AlertState, Organization
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv
import numpy as np

load_dotenv()  # reads .env into os.environ

# ===============================
# Flask App Initialization
# ===============================
app = Flask(__name__)

secret_key = os.environ.get("SECRET_KEY")

if not secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required.")

app.config["SECRET_KEY"] = secret_key

is_production = os.environ.get("FLASK_ENV") == "production"

# Secure session-cookie settings
app.config.update(
    SESSION_COOKIE_SECURE=is_production,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

database_url = os.environ.get("DATABASE_URL", "sqlite:///portfolio.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

# ✅ Bind SQLAlchemy to this app
db.init_app(app)

# ===============================
# Cache Setup
# ===============================
# Production on Render should use Redis via REDIS_URL.
# Local development should not fail if Redis is not running.
# If REDIS_URL is missing, or points to local Redis while not in production,
# fall back to Flask-Caching SimpleCache.

redis_url = os.environ.get("REDIS_URL")
use_redis_cache = bool(redis_url) and is_production

# Safety: if a copied local .env contains REDIS_URL=redis://127.0.0.1:6379
# or redis://localhost:6379, do not try to connect unless FLASK_ENV=production.
if use_redis_cache:
    cache_config = {
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_URL": redis_url,
        "CACHE_DEFAULT_TIMEOUT": 300,
        "CACHE_KEY_PREFIX": "portfolio-dashboard-live-v3:",
    }
else:
    cache_config = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 300,
        "CACHE_KEY_PREFIX": "portfolio-dashboard-local-dev:",
    }

cache = Cache(config=cache_config)
cache.init_app(app)

migrate = Migrate(app, db)

# ----- set ticker alarms --------
ALERT_THRESHOLD = -10 # if this is changed, also make changes in teckers_partial.html
RESET_THRESHOLD = -9
COOLDOWN_MINUTES = 60


# ----------------------------
# Helper function to get live prices
# ----------------------------
@cache.memoize(timeout=60)
def get_live_prices(tickers_tuple):
    """Fetch latest available prices and month-to-date percentage changes.

    The MTD baseline is the final valid daily close before the current month.
    The current price is taken from the latest available one-minute quote,
    with the latest daily close used as a fallback.

    Returns:
        {
            ticker: {
                "price": float | None,
                "baseline": float | None,
                "pct": float | None,
            }
        }
    """
    tickers = list(tickers_tuple)

    if not tickers:
        return {}

    today = pd.Timestamp.today().normalize()
    month_start = today.replace(day=1)
    history_start = month_start - pd.Timedelta(days=10)
    history_end = today + pd.Timedelta(days=1)

    # Daily prices provide the previous month-end baseline and a safe fallback.
    daily_data = yf.download(
        tickers,
        start=history_start,
        end=history_end,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=False,
    )

    # Intraday prices provide the latest available current price.
    # Yahoo quotes may be delayed depending on the exchange and instrument.
    intraday_data = yf.download(
        tickers,
        period="1d",
        interval="1m",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=False,
        prepost=False,
    )

    live_data = {}

    for ticker in tickers:
        try:
            daily_df = (
                daily_data[ticker]
                if isinstance(daily_data.columns, pd.MultiIndex)
                else daily_data
            )
            daily_close = pd.to_numeric(
                daily_df["Close"], errors="coerce"
            ).dropna().copy()

            if not daily_close.empty:
                daily_close.index = (
                    pd.to_datetime(daily_close.index)
                    .tz_localize(None)
                )

            if daily_close.empty:
                live_data[ticker] = {
                    "price": None,
                    "baseline": None,
                    "pct": None,
                }
                continue

            prior_month_closes = daily_close.loc[
                daily_close.index < month_start
            ]

            if not prior_month_closes.empty:
                month_baseline = float(prior_month_closes.iloc[-1])
            else:
                current_month_closes = daily_close.loc[
                    daily_close.index >= month_start
                ]
                if current_month_closes.empty:
                    live_data[ticker] = {
                        "price": round(float(daily_close.iloc[-1]), 2),
                        "baseline": None,
                        "pct": None,
                    }
                    continue
                month_baseline = float(current_month_closes.iloc[0])

            latest_price = None

            try:
                intraday_df = (
                    intraday_data[ticker]
                    if isinstance(intraday_data.columns, pd.MultiIndex)
                    else intraday_data
                )
                intraday_close = pd.to_numeric(
                    intraday_df["Close"], errors="coerce"
                ).dropna()

                if not intraday_close.empty:
                    latest_price = float(intraday_close.iloc[-1])
            except (KeyError, TypeError, ValueError):
                latest_price = None

            if latest_price is None:
                latest_price = float(daily_close.iloc[-1])

            pct = (
                ((latest_price / month_baseline) - 1) * 100
                if month_baseline != 0
                else None
            )

            live_data[ticker] = {
                "price": round(latest_price, 2),
                "baseline": round(month_baseline, 6),
                "pct": round(pct, 2) if pct is not None else None,
            }

        except Exception:
            app.logger.exception(
                "Failed to fetch current MTD price for ticker %s",
                ticker,
            )
            live_data[ticker] = {
                "price": None,
                "baseline": None,
                "pct": None,
            }

    return live_data



def calculate_return_since_purchase(latest_price, buy_price):
    """Return the ticker's percentage movement from its recorded buy price."""
    if latest_price is None or buy_price is None:
        return None

    try:
        buy_price = float(buy_price)
        if buy_price == 0:
            return None

        return round(((float(latest_price) / buy_price) - 1) * 100, 2)
    except (TypeError, ValueError):
        return None


def get_rolling_portfolio(organization_id, create=False):
    """Return the permanent portfolio container used by managed holdings."""
    portfolio = Portfolio.query.filter_by(
        organization_id=organization_id,
        month="rolling",
    ).first()
    if not portfolio and create:
        portfolio = Portfolio(
            organization_id=organization_id,
            month="rolling",
            start_date=datetime.utcnow().date(),
        )
        db.session.add(portfolio)
        db.session.flush()
    return portfolio


def active_holdings_query(organization_id):
    return (
        PortfolioTicker.query
        .join(Portfolio, PortfolioTicker.portfolio_id == Portfolio.id)
        .filter(
            Portfolio.organization_id == organization_id,
            PortfolioTicker.date_sold.is_(None),
        )
    )


def clear_portfolio_caches():
    cache.delete_memoized(get_dashboard_data)
    cache.delete_memoized(get_live_prices)


# ----------------------------
# Send Emails When TP/SL hit
# ----------------------------
# Unified email sender
# ----------------------------
# Send Emails via SendGrid API
# ----------------------------
def send_portfolio_alert_thread(subject, body, organization_id):
    """Send alert to all configured emails using SendGrid API."""
    with app.app_context():
        emails = [
            e.email
            for e in AlertEmail.query.filter_by(
                organization_id=organization_id
            ).all()
        ]
        if not emails:
            app.logger.warning(
                "No alert emails configured for organization %s.",
                organization_id,
            )
            return False

        try:
            sg = SendGridAPIClient(os.environ.get("SENDGRID_API_KEY"))

            message = Mail(
                from_email=os.environ.get("ALERT_FROM_EMAIL"),
                to_emails=emails,
                subject=subject,
                plain_text_content=body,
            )

            sg.send(message)

            app.logger.info(
                "SendGrid email sent to %s recipient(s).",
                len(emails),
            )
            return True

        except Exception:
            app.logger.exception(
                "SendGrid email failed for organization %s.",
                organization_id,
            )
            return False


def send_portfolio_alert_async(subject, body, organization_id):
    """Run the alert in a background thread."""
    thread = threading.Thread(target=send_portfolio_alert_thread, args=(subject, body, organization_id))
    thread.start()


def send_test_email_async(organization_id):
    """Send a test email to all recipients."""
    subject = "📈 Test Alert – Portfolio Dashboard"
    body = "This is a test alert from your portfolio dashboard."
    send_portfolio_alert_async(subject, body, organization_id)


# ----------------------------
# Check TP/SL
# ----------------------------
def check_and_send_portfolio_alerts(settings, portfolio_pct, organization_id):
    if not settings:
        return

    # 🔒 CRITICAL FIX
    if portfolio_pct is None:
        return
    
    # 🚫 Block Day 1 / no movement
    if abs(portfolio_pct) < 0.0001:
        return

    # TP1
    if settings.tp1 is not None and not settings.tp1_hit and portfolio_pct >= settings.tp1:
        send_portfolio_alert_async(
            subject="📈 Portfolio TP1 Hit",
            body=f"Portfolio has reached TP1 at {portfolio_pct:.2f}%.",
            organization_id=organization_id
        )
        settings.tp1_hit = True

    # TP2
    if settings.tp2 is not None and not settings.tp2_hit and portfolio_pct >= settings.tp2:
        send_portfolio_alert_async(
            subject="📈 Portfolio TP2 Hit",
            body=f"Portfolio has reached TP2 at {portfolio_pct:.2f}%.",
            organization_id=organization_id
        )
        settings.tp2_hit = True

    # TP3
    if settings.tp3 is not None and not settings.tp3_hit and portfolio_pct >= settings.tp3:
        send_portfolio_alert_async(
            subject="📈 Portfolio TP3 Hit",
            body=f"Portfolio has reached TP3 at {portfolio_pct:.2f}%.",
            organization_id=organization_id
        )
        settings.tp3_hit = True

    # Stop Loss
    if settings.stop_loss is not None and not settings.sl_hit and portfolio_pct <= settings.stop_loss:
        send_portfolio_alert_async(
            subject="🚨 Portfolio Stop Loss Hit",
            body=f"Portfolio has hit Stop Loss at {portfolio_pct:.2f}%.",
            organization_id=organization_id
        )
        settings.sl_hit = True

    db.session.commit()


# ----------------------------
# Check ticker -10% alert
# ----------------------------
def check_ticker_drawdown_alerts(tickers_data, organization_id):

    triggered = []
    now = datetime.now()

    for t in tickers_data:
        ticker = t.get("ticker")
        pct = t.get("pct")
        sold = t.get("sold")

        # ❌ Skip invalid or sold
        if pct is None or sold:
            continue

        alert = AlertState.query.filter_by(
            organization_id=organization_id,
            ticker=ticker
        ).first()

        # =========================
        # 🚨 ALERT CONDITION
        # =========================
        if pct <= ALERT_THRESHOLD:

            send_alert = False

            if not alert:
                send_alert = True
            else:
                if now - alert.last_alert_time > timedelta(minutes=COOLDOWN_MINUTES):
                    send_alert = True

            if send_alert:
                triggered.append(f"{ticker} ({pct:.2f}%)")

                if not alert:
                    alert = AlertState(
                        organization_id=organization_id,
                        ticker=ticker
                    )

                alert.last_alert_time = now
                db.session.add(alert)

        # =========================
        # 🔄 RESET (HYSTERESIS)
        # =========================
        elif pct >= RESET_THRESHOLD:
            if alert:
                db.session.delete(alert)

    db.session.commit()

    if not triggered:
        return []

    subject = "🚨 Ticker Alert: -10% Drawdown"
    body = "The following tickers are below -10%:\n\n" + "\n".join(triggered)

    send_portfolio_alert_async(subject, body, organization_id)

    return triggered


def send_password_reset_email(to_email, reset_link):

    subject = "Reset your Portfolio Dashboard password"

    body = f"""
            Hello,

            Click the link below to reset your password:

            {reset_link}

            This link expires in 30 minutes.

            If you did not request this, please ignore this email.
            """

    message = Mail(
        from_email=os.getenv("ALERT_FROM_EMAIL"),
        to_emails=to_email,
        subject=subject,
        plain_text_content=body
    )

    
    try:
        sg = SendGridAPIClient(
            os.getenv("SENDGRID_API_KEY")
        )

        sg.send(message)

        app.logger.info(
            "Password-reset email sent successfully",
        )

        return True

    except Exception:
        app.logger.exception(
            "Failed to send password-reset email."
        )
        return False


@cache.memoize(timeout=60)
def get_dashboard_data(current_month, organization_id):
    """Build the dashboard using each ticker's uploaded purchase price."""
    chart_html = None
    tickers_data = []
    portfolio_pct = None
    last_updated = datetime.now().strftime("%H:%M")

    try:
        month_start = pd.Timestamp(f"{current_month}-01").normalize()
    except (TypeError, ValueError):
        month_start = pd.Timestamp.today().normalize().replace(day=1)

    today = pd.Timestamp.today().normalize()
    month_end = (month_start + pd.offsets.MonthEnd(1)).normalize()
    display_end = min(today, month_end)
    all_days = pd.date_range(start=month_start, end=month_end, freq="D")

    active_rows = active_holdings_query(organization_id).order_by(
        PortfolioTicker.date_bought.asc(),
        PortfolioTicker.ticker.asc(),
    ).all()
    # A legacy monthly upload may have left the same unsold ticker in more
    # than one month. Use its newest purchase record until an admin removes
    # the duplicate from the management screen.
    db_tickers_by_symbol = {}
    for holding in active_rows:
        db_tickers_by_symbol[holding.ticker] = holding
    db_tickers = list(db_tickers_by_symbol.values())

    if not db_tickers:
        return {
            "message": "No active holdings. Add the first holding from the Admin Panel.",
            "chart_html": None,
            "tickers": [],
            "portfolio_pct": None,
            "last_updated": last_updated,
            "top_contrib": [],
            "bottom_contrib": []
        }

    tickers = [t.ticker for t in db_tickers]

    if not tickers:
        return {
            "message": "The rolling portfolio contains no active holdings.",
            "chart_html": None,
            "tickers": [],
            "portfolio_pct": None,
            "last_updated": last_updated,
            "top_contrib": [],
            "bottom_contrib": []
        }

    live_prices = get_live_prices(tuple(sorted(tickers)))
    history_start = month_start - pd.Timedelta(days=10)
    history_end = display_end + pd.Timedelta(days=1)

    hist_data = yf.download(
        tickers,
        start=history_start,
        end=history_end,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False
    )

    normalized_prices = pd.DataFrame(index=all_days, columns=tickers, dtype=float)

    if not hist_data.empty:
        if isinstance(hist_data.columns, pd.MultiIndex):
            close_prices = hist_data["Close"].copy()
        else:
            close_prices = hist_data[["Close"]].rename(columns={"Close": tickers[0]})

        close_prices.index = pd.to_datetime(close_prices.index).tz_localize(None)

        for ticker_record in db_tickers:
            ticker = ticker_record.ticker
            if ticker not in close_prices.columns:
                continue

            series = pd.to_numeric(close_prices[ticker], errors="coerce").dropna()
            if series.empty:
                continue

            try:
                buy_price = float(ticker_record.buy_price)
            except (TypeError, ValueError):
                app.logger.warning(
                    "Invalid buy price for ticker %s: %s",
                    ticker,
                    ticker_record.buy_price,
                )
                continue

            if buy_price <= 0:
                continue

            purchase_date = pd.Timestamp(
                ticker_record.date_bought
            ).normalize()

            # The chart is limited to the selected portfolio month, but each
            # ticker is measured from its uploaded purchase price.
            chart_start = max(month_start, purchase_date)

            # Anchor the ticker at 1.00 on its purchase date (or at month start
            # when it was bought before this month).
            normalized_prices.loc[chart_start, ticker] = 1.0

            purchase_series = series.loc[
                (series.index >= chart_start)
                & (series.index <= display_end)
            ] / buy_price

            if ticker_record.date_sold:
                sold_date = pd.Timestamp(
                    ticker_record.date_sold
                ).normalize()
                purchase_series = purchase_series.loc[
                    purchase_series.index <= sold_date
                ]

            normalized_prices.loc[
                purchase_series.index,
                ticker
            ] = purchase_series.values

    # Replace/add today's final chart point with the latest available price.
    # Every ticker remains measured from its uploaded purchase price.
    for ticker_record in db_tickers:
        ticker = ticker_record.ticker
        live = live_prices.get(ticker, {})
        latest_price = live.get("price")

        try:
            buy_price = float(ticker_record.buy_price)
        except (TypeError, ValueError):
            continue

        if buy_price <= 0:
            continue

        purchase_date = pd.Timestamp(
            ticker_record.date_bought
        ).normalize()

        sold_date = (
            pd.Timestamp(ticker_record.date_sold).normalize()
            if ticker_record.date_sold
            else None
        )

        if (
            latest_price is None
            or today < purchase_date
            or (sold_date is not None and sold_date < today)
        ):
            continue

        chart_start = max(month_start, purchase_date)
        normalized_prices.loc[chart_start, ticker] = 1.0
        normalized_prices.loc[today, ticker] = (
            float(latest_price) / buy_price
        )

    normalized_prices = normalized_prices.ffill()
    normalized_prices.loc[normalized_prices.index > display_end] = np.nan

    active_counts = normalized_prices.notna().sum(axis=1).replace(0, np.nan)
    portfolio_index = normalized_prices.sum(axis=1, min_count=1) / active_counts
    portfolio_index = pd.to_numeric(portfolio_index, errors="coerce")

    latest_day = normalized_prices.dropna(how="all").last_valid_index()
    return_map = {}
    contrib_map = {}
    weight_map = {}

    if latest_day is not None:
        latest_values = normalized_prices.loc[latest_day].dropna()
        num_active = len(latest_values)

        if num_active:
            weight = 1 / num_active
            for ticker, normalized_value in latest_values.items():
                return_pct = (float(normalized_value) - 1) * 100
                return_map[ticker] = round(return_pct, 2)
                contrib_map[ticker] = round(return_pct * weight, 2)
                weight_map[ticker] = round(weight * 100, 2)

            portfolio_pct = round(sum(contrib_map.values()), 2)

    for ticker_record in db_tickers:
        live = live_prices.get(ticker_record.ticker, {})
        latest_price = live.get("price")
        purchase_return = calculate_return_since_purchase(
            latest_price,
            ticker_record.buy_price,
        )

        tickers_data.append({
            "ticker": ticker_record.ticker,
            "market_index": ticker_record.market_index,
            "price": latest_price,
            "pct": purchase_return,
            "return_pct": purchase_return,
            "contribution_pct": contrib_map.get(ticker_record.ticker),
            "sold": bool(ticker_record.date_sold),
            "weight": weight_map.get(ticker_record.ticker)
        })

    valid_contributors = [
        item for item in tickers_data
        if item.get("contribution_pct") is not None
    ]

    top_contrib = sorted(
        valid_contributors,
        key=lambda item: item["contribution_pct"],
        reverse=True
    )[:5]

    bottom_contrib = sorted(
        valid_contributors,
        key=lambda item: item["contribution_pct"]
    )[:5]

    total_absolute_contribution = sum(
        abs(item["contribution_pct"]) for item in valid_contributors
    )

    for item in top_contrib + bottom_contrib:
        item["share_of_portfolio"] = (
            round(abs(item["contribution_pct"]) / total_absolute_contribution * 100, 1)
            if total_absolute_contribution
            else 0.0
        )

    indices = sorted({t.market_index for t in db_tickers if t.market_index})
    index_map = {
        "DOW": "^DJI",
        "NASDAQ": "^IXIC",
        "FTSE": "^FTSE",
        "S&P500": "^GSPC",
        "NIKKEI": "^N225",
        "DAX": "^GDAXI"
    }
    benchmark_symbols = [
        index_map[index_name.upper()]
        for index_name in indices
        if index_name.upper() in index_map
    ]

    benchmarks = pd.DataFrame(index=all_days)

    if benchmark_symbols:
        raw_benchmarks = yf.download(
            benchmark_symbols,
            start=history_start,
            end=history_end,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False
        )

        if not raw_benchmarks.empty:
            if isinstance(raw_benchmarks.columns, pd.MultiIndex):
                benchmark_closes = raw_benchmarks["Close"].copy()
            else:
                benchmark_closes = raw_benchmarks[["Close"]].rename(
                    columns={"Close": benchmark_symbols[0]}
                )

            benchmark_closes.index = pd.to_datetime(benchmark_closes.index).tz_localize(None)

            for symbol in benchmark_symbols:
                if symbol not in benchmark_closes.columns:
                    continue

                series = pd.to_numeric(benchmark_closes[symbol], errors="coerce").dropna()
                if series.empty:
                    continue

                prior_closes = series.loc[series.index < month_start]
                if not prior_closes.empty:
                    baseline = float(prior_closes.iloc[-1])
                else:
                    current_closes = series.loc[series.index >= month_start]
                    if current_closes.empty:
                        continue
                    baseline = float(current_closes.iloc[0])

                if baseline == 0:
                    continue

                benchmarks.loc[month_start, symbol] = 1.0

                month_series = series.loc[
                    (series.index >= month_start) & (series.index <= display_end)
                ] / baseline
                benchmarks.loc[month_series.index, symbol] = month_series.values

        benchmark_live_prices = get_live_prices(
            tuple(sorted(benchmark_symbols))
        )

        for symbol in benchmark_symbols:
            live = benchmark_live_prices.get(symbol, {})
            latest_price = live.get("price")
            baseline = live.get("baseline")

            if (
                latest_price is None
                or baseline is None
                or baseline == 0
            ):
                continue

            benchmarks.loc[month_start, symbol] = 1.0
            benchmarks.loc[today, symbol] = (
                float(latest_price) / float(baseline)
            )

    benchmarks = benchmarks.ffill()
    benchmarks.loc[benchmarks.index > display_end] = np.nan

    message = None
    if portfolio_index.dropna().empty:
        message = "No trading data available yet for this month."

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=portfolio_index.index,
        y=portfolio_index.values,
        mode="lines+markers",
        name="Portfolio"
    ))

    for index_name, symbol in index_map.items():
        if symbol in benchmarks.columns:
            fig.add_trace(go.Scatter(
                x=benchmarks.index,
                y=benchmarks[symbol],
                mode="lines+markers",
                name=index_name
            ))

    fig.update_layout(
        title={
            "text": f"Equal Weight Portfolio Performance for {month_start.strftime('%B %Y')}",
            "x": 0.5,
            "xanchor": "center",
            "yanchor": "top",
            "font": dict(size=20, family="Arial, sans-serif", color="black")
        },
        xaxis_title="Date",
        yaxis_title="Index (Purchase Price=1)",
        template="plotly_white",
        xaxis=dict(tickformat="%d-%m", tickmode="auto", nticks=10)
    )

    all_values = pd.concat([
        portfolio_index.dropna(),
        benchmarks.stack().dropna() if not benchmarks.empty else pd.Series(dtype=float)
    ])

    if not all_values.empty:
        ymin = float(all_values.min())
        ymax = float(all_values.max())
        value_range = ymax - ymin
        padding = max(value_range * 0.10, 0.005)
        fig.update_layout(yaxis=dict(range=[ymin - padding, ymax + padding]))

    chart_html = pio.to_html(fig, full_html=False)

    return {
        "chart_html": chart_html,
        "tickers": tickers_data,
        "portfolio_pct": portfolio_pct,
        "last_updated": last_updated,
        "message": message,
        "top_contrib": top_contrib,
        "bottom_contrib": bottom_contrib
    }


# ----------------------------
# Main dashboard route
# ----------------------------
@app.route("/dashboard")
def dashboard():
    
    if "user_id" not in session:
        return redirect("/login")

    user = current_user()

    if not user:
        return redirect("/login")
    
    organization_id = user.organization_id

    today = pd.Timestamp.today().normalize()
    current_month = today.strftime("%Y-%m")

    data = get_dashboard_data(current_month, organization_id)

    # ALWAYS FRESH SETTINGS, BUT ORGANIZATION-SCOPED
    settings = PortfolioSettings.query.filter_by(
        organization_id=organization_id
    ).first()

    if settings:
        db.session.refresh(settings)

    #Do NOT pass PortfolioSettings through cached function → always fetch fresh in route



    # ✅ RUN ALERTS OUTSIDE CACHE
    check_ticker_drawdown_alerts(
        data["tickers"], 
        organization_id
        )
    
    check_and_send_portfolio_alerts(settings, 
                                    data["portfolio_pct"], 
                                    organization_id)
      
    return render_template(
        "dashboard.html",
        current_month=current_month,
        portfolio_settings=settings,
        chart_html=data["chart_html"],
        tickers=data["tickers"],
        portfolio_pct=data["portfolio_pct"],
        last_updated=data["last_updated"],
        message=data["message"],
        top_contrib=data["top_contrib"],            # 🔥 NEW
        bottom_contrib=data["bottom_contrib"]       # 🔥 NEW
    )


@app.route("/chart-refresh")
def chart_refresh():

    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user = current_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    today = pd.Timestamp.today().normalize()
    current_month = today.strftime("%Y-%m")

    organization_id = user.organization_id
    data = get_dashboard_data(current_month, organization_id)

    if not data:
        return jsonify({"error": "No data"}), 404

    last_updated = datetime.now().strftime("%H:%M")

    return jsonify({
        "chart_html": data["chart_html"],
        "portfolio_pct": data["portfolio_pct"],
        "last_updated": last_updated,
        "message": data.get("message")
    })


@app.route("/tickers-refresh")
def tickers_refresh():

    user = current_user()

    if not user:
        return "", 401

    organization_id = user.organization_id

    today = pd.Timestamp.today().normalize()
    current_month = today.strftime("%Y-%m")

    ticker_records = active_holdings_query(organization_id).order_by(
        PortfolioTicker.ticker.asc()
    ).all()

    if not ticker_records:
        return ""

    tickers = [t.ticker for t in ticker_records]
    live_prices = get_live_prices(tuple(sorted(tickers)))  # ✅ tuple for cache consistency

    tickers_data = []

    for t in ticker_records:
        lp = live_prices.get(t.ticker, {})
        latest_price = lp.get("price")
        purchase_return = calculate_return_since_purchase(
            latest_price,
            t.buy_price,
        )

        tickers_data.append({
            "ticker": t.ticker,
            "market_index": t.market_index,
            "price": latest_price,
            "pct": purchase_return,
            "sold": bool(t.date_sold)
        })

    last_updated = datetime.now().strftime("%H:%M")

    return render_template(
        "tickers_partial.html",
        tickers=tickers_data,
        last_updated=last_updated,
    )


@app.context_processor
def inject_user():

    return {
        "current_user": current_user(),
        "current_org": current_org(),
        "is_admin": is_admin()
    }

# -------------------------------
# ADMIN PANEL
# -------------------------------

@app.route("/admin")
def admin():

    if not is_admin():
        abort(403)

    user = current_user()
    organization_id = user.organization_id

    tickers = active_holdings_query(organization_id).order_by(
        PortfolioTicker.date_bought.asc(),
        PortfolioTicker.ticker.asc(),
    ).all()
    sold_tickers = (
        PortfolioTicker.query
        .join(Portfolio, PortfolioTicker.portfolio_id == Portfolio.id)
        .filter(
            Portfolio.organization_id == organization_id,
            PortfolioTicker.date_sold.is_not(None),
        )
        .order_by(PortfolioTicker.date_sold.desc())
        .all()
    )

    emails = AlertEmail.query.filter_by(
        organization_id=organization_id
    ).all()

    settings = PortfolioSettings.query.filter_by(
        organization_id=organization_id
    ).first()

    # =========================
    # USERS
    # =========================

    users = User.query.filter_by(
        organization_id=organization_id
    ).order_by(User.id.asc()).all()

    existing_admin = User.query.filter_by(
        organization_id=organization_id,
        role="admin"
    ).first()

    return render_template(
        "admin.html",
        tickers=tickers,
        sold_tickers=sold_tickers,
        emails=emails,
        settings=settings,
        users=users,
        existing_admin=existing_admin
    )


def parse_positive_float(value, field_name, required=True):
    if value in (None, "") and not required:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number.")
    if number <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return number


def parse_form_date(value, field_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {field_name}.")


@app.route("/admin/holdings/add", methods=["POST"])
def add_holding():
    if not is_admin():
        abort(403)

    user = current_user()
    ticker = (request.form.get("ticker") or "").strip().upper()
    market_index = (request.form.get("market_index") or "").strip().upper()
    company_name = (request.form.get("company_name") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None

    if not ticker or len(ticker) > 10 or not all(c.isalnum() or c in ".-" for c in ticker):
        flash("Enter a valid ticker symbol (maximum 10 characters).", "danger")
        return redirect("/admin")

    if active_holdings_query(user.organization_id).filter(
        PortfolioTicker.ticker == ticker
    ).first():
        flash(f"{ticker} is already an active holding.", "warning")
        return redirect("/admin")

    try:
        buy_price = parse_positive_float(request.form.get("buy_price"), "Purchase price")
        quantity = parse_positive_float(request.form.get("quantity"), "Quantity", required=False)
        date_bought = parse_form_date(request.form.get("date_bought"), "purchase date")
        if date_bought > datetime.utcnow().date():
            raise ValueError("Purchase date cannot be in the future.")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect("/admin")

    portfolio = get_rolling_portfolio(user.organization_id, create=True)
    db.session.add(PortfolioTicker(
        ticker=ticker,
        company_name=company_name,
        market_index=market_index or None,
        buy_price=buy_price,
        quantity=quantity,
        date_bought=date_bought,
        notes=notes,
        portfolio_id=portfolio.id,
    ))
    db.session.commit()
    clear_portfolio_caches()
    flash(f"{ticker} added to the active portfolio.", "success")
    return redirect("/admin")


def organization_holding_or_404(holding_id, organization_id):
    holding = (
        PortfolioTicker.query
        .join(Portfolio, PortfolioTicker.portfolio_id == Portfolio.id)
        .filter(
            PortfolioTicker.id == holding_id,
            Portfolio.organization_id == organization_id,
        )
        .first()
    )
    if not holding:
        abort(404)
    return holding


@app.route("/admin/holdings/<int:holding_id>/edit", methods=["POST"])
def edit_holding(holding_id):
    if not is_admin():
        abort(403)
    user = current_user()
    holding = organization_holding_or_404(holding_id, user.organization_id)
    if not holding.is_active:
        flash("Sold holdings cannot be edited as active positions.", "warning")
        return redirect("/admin")

    try:
        holding.buy_price = parse_positive_float(request.form.get("buy_price"), "Purchase price")
        holding.quantity = parse_positive_float(request.form.get("quantity"), "Quantity", required=False)
        holding.date_bought = parse_form_date(request.form.get("date_bought"), "purchase date")
        if holding.date_bought > datetime.utcnow().date():
            raise ValueError("Purchase date cannot be in the future.")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect("/admin")

    holding.company_name = (request.form.get("company_name") or "").strip() or None
    holding.market_index = (request.form.get("market_index") or "").strip().upper() or None
    holding.notes = (request.form.get("notes") or "").strip() or None
    db.session.commit()
    clear_portfolio_caches()
    flash(f"{holding.ticker} updated.", "success")
    return redirect("/admin")


@app.route("/admin/holdings/<int:holding_id>/sell", methods=["POST"])
def sell_holding(holding_id):
    if not is_admin():
        abort(403)
    user = current_user()
    holding = organization_holding_or_404(holding_id, user.organization_id)
    if not holding.is_active:
        flash(f"{holding.ticker} has already been sold.", "warning")
        return redirect("/admin")
    try:
        sale_price = parse_positive_float(request.form.get("sale_price"), "Sale price")
        sale_date = parse_form_date(request.form.get("date_sold"), "sale date")
        if sale_date < holding.date_bought:
            raise ValueError("Sale date cannot be before the purchase date.")
        if sale_date > datetime.utcnow().date():
            raise ValueError("Sale date cannot be in the future.")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect("/admin")

    holding.sale_price = sale_price
    holding.date_sold = sale_date
    db.session.commit()
    clear_portfolio_caches()
    flash(f"{holding.ticker} marked as sold and retained in history.", "success")
    return redirect("/admin")


@app.route("/admin/holdings/<int:holding_id>/delete", methods=["POST"])
def delete_holding(holding_id):
    if not is_admin():
        abort(403)
    user = current_user()
    holding = organization_holding_or_404(holding_id, user.organization_id)
    ticker = holding.ticker
    db.session.delete(holding)
    db.session.commit()
    clear_portfolio_caches()
    flash(f"Erroneous {ticker} entry permanently deleted.", "success")
    return redirect("/admin")
    
    
@app.route("/admin/upload", methods=["POST"])
def upload_portfolio():

    if not is_admin():
        abort(403)

    user = current_user()
    organization_id = user.organization_id

    file = request.files.get("file")

    if not file or not file.filename:
        flash("No file uploaded.", "danger")
        return redirect("/admin")

    allowed_extensions = (".xlsx", ".xlsm")

    if not file.filename.lower().endswith(allowed_extensions):
        flash("Only .xlsx and .xlsm files are supported.", "danger")
        return redirect("/admin")

    try:
        df = pd.read_excel(file, engine="openpyxl")
    except Exception:
        app.logger.exception("Failed to read uploaded Excel file")
        flash("The Excel file could not be read. Please check the file format.", "danger")
        return redirect("/admin")

    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
    )

    df["ticker"] = df["ticker"].str.strip()
    df["date_bought"] = pd.to_datetime(df["date_bought"]).dt.date
    df["date_sold"] = df["date_sold"].replace({
        pd.NaT: None,
        float("nan"): None
    })

    df = df.where(pd.notnull(df), None)
    df = df.dropna(subset=["ticker"])

    today = pd.Timestamp.today().normalize()
    portfolio_month = today.strftime("%Y-%m")

    portfolio = Portfolio.query.filter_by(
        organization_id=organization_id,
        month=portfolio_month
    ).first()

    if not portfolio:
        portfolio = Portfolio(
            organization_id=organization_id,
            month=portfolio_month,
            start_date=today
        )

        db.session.add(portfolio)
        db.session.commit()

    PortfolioTicker.query.filter_by(
        portfolio_id=portfolio.id
    ).delete()

    for _, row in df.iterrows():
        db.session.add(
            PortfolioTicker(
                ticker=row["ticker"].upper(),
                market_index=row["index"].upper(),
                portfolio_id=portfolio.id,
                buy_price=row["buy_price"],
                date_bought=row["date_bought"],
                date_sold=(
                    row.get("date_sold")
                    if pd.notna(row.get("date_sold"))
                    else None
                )
            )
        )

    settings = PortfolioSettings.query.filter_by(
        organization_id=organization_id
    ).first()

    if settings:
        settings.tp1_hit = False
        settings.tp2_hit = False
        settings.tp3_hit = False
        settings.sl_hit = False

    db.session.commit()

    cache.delete_memoized(get_dashboard_data)

    flash("Portfolio uploaded successfully.", "success")
    return redirect("/admin")


#++++++++++++++++ SET TPs and SL
@app.route("/admin/set_targets", methods=["POST"])
def admin_set_targets():

    if not is_admin():
        abort(403)

    user = current_user()
    organization_id = user.organization_id

    try:
        tp1 = float(request.form.get("tp1"))
        tp2 = float(request.form.get("tp2"))
        tp3 = float(request.form.get("tp3"))
        stop_loss = float(request.form.get("stop_loss"))

    except (TypeError, ValueError):
        flash("Invalid TP/SL values.", "danger")
        return redirect("/admin")

    if not (tp1 <= tp2 <= tp3):
        flash("TP must be ascending: TP1 ≤ TP2 ≤ TP3", "danger")
        return redirect("/admin")

    if stop_loss > tp1:
        flash("Stop loss must be ≤ TP1", "danger")
        return redirect("/admin")

    settings = PortfolioSettings.query.filter_by(
        organization_id=organization_id
    ).first()

    if not settings:
        settings = PortfolioSettings(
            organization_id=organization_id,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            stop_loss=stop_loss,
            tp1_hit=False,
            tp2_hit=False,
            tp3_hit=False,
            sl_hit=False
        )
        db.session.add(settings)
    else:
        settings.tp1 = tp1
        settings.tp2 = tp2
        settings.tp3 = tp3
        settings.stop_loss = stop_loss
        settings.tp1_hit = False
        settings.tp2_hit = False
        settings.tp3_hit = False
        settings.sl_hit = False

    db.session.commit()

    cache.delete_memoized(get_dashboard_data)

    flash("Portfolio TP/SL updated successfully", "success")
    return redirect("/admin")


#++++++++++++++ ADD EMAILS
@app.route("/admin/add_emails", methods=["POST"])
def admin_add_emails():

    if not is_admin():
        abort(403)

    user = current_user()
    organization_id = user.organization_id

    added_count = 0

    for i in range(5):
        email = request.form.get(f"email_{i}")

        if not email:
            continue

        email = email.strip().lower()

        existing = AlertEmail.query.filter_by(
            organization_id=organization_id,
            email=email
        ).first()

        if existing:
            continue

        db.session.add(
            AlertEmail(
                organization_id=organization_id,
                email=email
            )
        )

        added_count += 1

    db.session.commit()
    cache.delete_memoized(get_dashboard_data)

    flash(f"{added_count} alert email(s) saved.", "success")
    return redirect("/admin")


#+++++++++++++DELETE EMAILS
@app.route("/admin/delete_email/<int:email_id>", methods=["POST"])
def admin_delete_email(email_id):

    if not is_admin():
        abort(403)

    user = current_user()

    email = AlertEmail.query.filter_by(
        id=email_id,
        organization_id=user.organization_id
    ).first()

    if not email:
        flash("Email not found.", "warning")
        return redirect("/admin")

    email_address = email.email

    db.session.delete(email)
    db.session.commit()

    cache.delete_memoized(get_dashboard_data)

    flash(f"{email_address} removed.", "success")
    return redirect("/admin")


#+++++++++++++ADMIN CREATE USERS (viewers)
@app.route("/admin/create-user", methods=["POST"])
def admin_create_user():

    if not is_admin():
        abort(403)

    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "viewer").strip()
    email = request.form.get("email", "").strip().lower()

    admin_user = current_user()
    organization_id = admin_user.organization_id

    if not username or not email or not password:
        flash("Username, email and password are required.", "danger")
        return redirect("/admin")

    if role not in ["admin", "viewer"]:
        flash("Invalid role selected.", "danger")
        return redirect("/admin")

    existing_user = User.query.filter_by(
        username=username,
        organization_id=organization_id
    ).first()

    if existing_user:
        flash("Username already exists.", "warning")
        return redirect("/admin")
    
    existing_email = User.query.filter_by(
        email=email,
        organization_id=organization_id
    ).first()

    if existing_email:
        flash("Email already exists for this company.", "warning")
        return redirect("/admin")

    # Enforce only one admin per organization
    if role == "admin":
        existing_admin = User.query.filter_by(
            organization_id=organization_id,
            role="admin"
        ).first()

        if existing_admin:
            flash("This company already has an admin.", "danger")
            return redirect("/admin")

    # ✅ Limit viewers to 5 per organization
    if role == "viewer":
        viewer_count = User.query.filter_by(
            organization_id=organization_id,
            role="viewer"
        ).count()

        if viewer_count >= 5:
            flash("You can only add up to 5 users for this company.", "warning")
            return redirect("/admin")

    new_user = User(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        organization_id=organization_id
    )

    db.session.add(new_user)
    db.session.commit()

    flash("User created successfully.", "success")
    return redirect("/admin")


#+++++++++++++ADMIN DELETE USERS (viewers)
@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
def admin_delete_user(user_id):

    if not is_admin():
        abort(403)

    admin_user = current_user()
    organization_id = admin_user.organization_id

    user_to_delete = User.query.filter_by(
        id=user_id,
        organization_id=organization_id
    ).first()

    if not user_to_delete:
        flash("User not found.", "danger")
        return redirect("/admin")

    if user_to_delete.id == admin_user.id:
        flash("You cannot delete your own admin account.", "danger")
        return redirect("/admin")

    db.session.delete(user_to_delete)
    db.session.commit()

    flash("User deleted successfully.", "success")
    return redirect("/admin")


@app.route("/")
def welcome():
    return render_template("welcome.html")

# -------------------------------
# REGISTRATION 
# -------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":
        return render_template("register.html")

    organization_name = request.form.get("organization_name", "").strip()
    username = request.form.get("username", "").strip().lower()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not organization_name or not username or not email or not password:
        flash("All fields are required.", "register_error")
        return redirect("/register")

    # Check existing company
    existing_organization = Organization.query.filter(
        db.func.lower(Organization.name) == organization_name.lower()
    ).first()

    if existing_organization:
        flash(
            "This company is already registered. Please ask your company admin to create your user account.",
            "warning"
        )
        return redirect("/register")

    # Create organization
    organization = Organization(
        name=organization_name
    )

    db.session.add(organization)
    db.session.flush()

    # Check username only inside this new company
    existing_user = User.query.filter_by(
        username=username,
        organization_id=organization.id
    ).first()

    if existing_user:
        flash("Username already exists for this company.", "register_error")
        return redirect("/register")

    # Create first admin user
    user = User(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        organization_id=organization.id,
        role="admin"
    )

    try:    #debugging
        db.session.add(user)
        db.session.commit()

    except Exception:
        db.session.rollback()

        app.logger.exception(
            "Registration failed."
        )

        flash(
            "Registration failed. Please try again.",
            "register_error",
        )
        return redirect("/register")

    #db.session.add(user) - install after debug
    #db.session.commit()    - install after debug

    flash("Registration successful. Please log in.", "success")
    return redirect("/login")


# -------------------------------
# LOGIN 
# -------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":
        return render_template("login.html")

    organization_name = request.form.get("organization_name", "").strip()
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")

    if not organization_name or not username or not password:
        flash("All fields are required.", "login_error")
        return redirect("/login")

    organization = Organization.query.filter(
        db.func.lower(Organization.name) == organization_name.lower()
    ).first()

    if not organization:
        flash("Company not found. Please register first.", "login_error")
        return redirect("/login")

    user = User.query.filter_by(
        username=username,
        organization_id=organization.id
    ).first()

    if not user:
        flash("User not found for this company. Please ask your admin to create your account.", "login_error")
        return redirect("/login")

    if user and check_password_hash(user.password_hash, password):

        session.clear()

        session["user_id"] = user.id
        session["organization_id"] = user.organization_id
        session["role"] = user.role
        session["username"] = user.username

        return redirect("/dashboard")

    flash("Wrong company, username or password.", "login_error")
    return redirect("/login")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":
        organization_name = request.form.get("organization_name", "").strip()
        email = request.form.get("email", "").strip().lower()

        org = Organization.query.filter_by(name=organization_name).first()

        if org:
            user = User.query.filter_by(
                organization_id=org.id,
                email=email
            ).first()

            if user:
                token = get_serializer().dumps({
                    "user_id": user.id,
                    "email": user.email
                })

                reset_link = url_for(
                    "reset_password",
                    token=token,
                    _external=True
                )

                subject = "Reset your Portfolio Dashboard password"

                body = f"""
                        Hello {user.username},

                        Click the link below to reset your password:

                        {reset_link}

                        This link expires in 30 minutes.

                        If you did not request this, please ignore this email.
                """

                send_password_reset_email(
                    user.email,
                    reset_link
                )

        flash(
            "If the account exists, a password reset link has been sent.",
            "info"
        )
        return redirect("/login")

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):

    try:
        data = get_serializer().loads(token, max_age=1800)
    except SignatureExpired:
        flash("Password reset link has expired.", "danger")
        return redirect("/forgot-password")
    except BadSignature:
        flash("Invalid password reset link.", "danger")
        return redirect("/forgot-password")

    user = db.session.get(User, data["user_id"])

    if not user or user.email != data["email"]:
        flash("Invalid password reset link.", "danger")
        return redirect("/forgot-password")

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not password or not confirm_password:
            flash("Both password fields are required.", "danger")
            return redirect(request.url)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(request.url)

        user.password_hash = generate_password_hash(password)
        db.session.commit()

        flash("Password reset successful. Please log in.", "success")
        return redirect("/login")

    return render_template("reset_password.html")


def current_user():

    user_id = session.get("user_id")

    if not user_id:
        return None

    return db.session.get(User, user_id)


def current_org():

    org_id = session.get("organization_id")

    if not org_id:
        return None

    return db.session.get(Organization, org_id)


def is_admin():

    user = current_user()

    return user and user.role == "admin"


def get_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"])


# -------------------------------
# LOGOUT
# -------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


#*****************************
# TEST EMAIL=============
@app.route("/admin/test-email", methods=["POST"])
def admin_test_email():

    if not is_admin():
        abort(403)

    user = current_user()

    emails = AlertEmail.query.filter_by(
        organization_id=user.organization_id
    ).all()

    if not emails:
        flash("No alert emails configured.", "warning")
        return redirect("/admin")

    subject = "📈 Test Alert – Portfolio Dashboard"

    body = (
        "This is a SendGrid test email "
        "from your Portfolio Dashboard."
    )

    success = send_portfolio_alert_thread(
        subject,
        body,
        user.organization_id
    )

    if success:
        flash(
            "✅ Test email sent successfully via SendGrid!",
            "success"
        )
    else:
        flash(
            "❌ Failed to send test email. Check logs and API key.",
            "danger"
        )

    return redirect("/admin")


if __name__ == "__main__":
    app.run(debug=True)
