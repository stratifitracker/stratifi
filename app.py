# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, send_file,session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy import desc,func
from datetime import datetime
import yfinance as yf
import pandas as pd
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import cProfile
import io
import pstats
import csv
import os
import requests_cache
from datetime import timedelta
from dotenv import load_dotenv
from flask_mail import Mail, Message
import random
import string

# Configure requests_cache but avoid using it in `yf.download`
cached_session = requests_cache.CachedSession(
    'yfinance.cache',
    expire_after=timedelta(hours=1),
    allowable_methods=['GET'],
    stale_if_error=True
)
cached_session.headers['User-agent'] = 'my-program/1.0'

load_dotenv()
# In-memory cache for stock prices
price_cache = {}
last_cache_update = None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///portfolio.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


# Email configuration (update these with your actual email provider)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('EMAIL_USER')
app.config['MAIL_PASSWORD'] = os.getenv('EMAIL_PASS')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('EMAIL_USER')

mail = Mail(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    verification_code = db.Column(db.String(20), nullable=False)
    password_hash = db.Column(db.String(128))
    transactions = db.relationship('Transaction', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ticker = db.Column(db.String(10), nullable=False)
    name = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    price = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    transaction_type = db.Column(db.String(4), nullable=False)  # 'buy' or 'sell'
    status = db.Column(db.String(20), nullable=False, default='closed')  # e.g., 'open', 'closed'
    asset_type = db.Column(db.String(50), default='STOCK')  # 'STOCK' or 'ETF'/'FUND'

class StockPrice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(10), nullable=False)
    price = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    sector = db.Column(db.String(100))  # New field
    daily_change_pct = db.Column(db.Float)  # New field for daily % change

    __table_args__ = (
        db.Index('idx_ticker_date', 'ticker', 'date'),
    )

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def get_latest_price(ticker):
    """Get latest price for a ticker from cache or database"""
    # Check cache first
    if ticker in price_cache:
        return price_cache[ticker]
    
    # Try to get from database if not in cache
    result = StockPrice.query.filter_by(ticker=ticker).order_by(desc(StockPrice.date)).first()
    if result:
        # Update cache and return price
        price_cache[ticker] = result.price
        return result.price
    
    # Return None if price not found
    return None

def get_latest_price_info(ticker):
    """Get latest price info including price, sector, asset_type, and daily change"""
    # Try to get from database first (most recent)
    result = StockPrice.query.filter_by(ticker=ticker).order_by(desc(StockPrice.date)).first()
    if result:
        return {
            'price': result.price,
            'sector': result.sector or 'Unknown',
            'daily_change_pct': result.daily_change_pct
        }
    
    # Return default values if no data found, using get_latest_price for price
    return {
        'price': get_latest_price(ticker) or 0,
        'sector': 'Unknown',
        'daily_change_pct': 0
    }

def get_price_on_date(ticker, date):
    # Convert date to datetime.date if it's a datetime
    if isinstance(date, datetime):
        date = date.date()
    
    # Find the closest price record on or before the given date
    price_record = (
        StockPrice.query
        .filter(StockPrice.ticker == ticker, StockPrice.date <= date)
        .order_by(StockPrice.date.desc())
        .first()
    )
    
    if price_record:
        return price_record.price
    else:
        # If no historical price found, try using the latest price
        latest = get_latest_price(ticker)
        return latest if latest is not None else 0
    
# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('index.html')



def generate_code(length=6):
    return ''.join(random.choices(string.digits, k=length))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email')

        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('Email already in use')
            return redirect(url_for('register'))

        code = generate_code()

        # Send email
        try:
            msg = Message(
                subject="Your Stratifi Verification Code",
                recipients=[email]
            )

            msg.body = f"Hi {username}, your verification code is: {code}"
            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
                <h2 style="color: #007BFF;">Welcome to Stratifi, {username}!</h2>
                <p>Thank you for registering. Your verification code is:</p>
                <p style="font-size: 24px; font-weight: bold; color: #333; background-color: #f0f0f0; padding: 10px; border-radius: 5px; display: inline-block;">
                {code}
                </p>
                <p>Enter this code on the website to verify your email address.</p>
                <hr style="margin: 30px 0;">
                <p style="font-size: 12px; color: #888;">If you did not attempt to register, please disregard this email.</p>
                <p style="font-size: 12px;">– The Stratifi Team</p>
            </body>
            </html>
            """

            mail.send(msg)
        except Exception as e:
            flash(f"Failed to send email: {e}")
            return redirect(url_for('register'))

        # Temporarily store data in session
        session['pending_user'] = {
            'username': username,
            'password': password,
            'email': email,
            'verification_code': code
        }

        return redirect(url_for('verify_code'))

    return render_template('register.html')
@app.route('/verify_code', methods=['GET', 'POST'])

def verify_code():
    pending = session.get('pending_user')
    if not pending:
        flash("No registration in progress.")
        return redirect(url_for('register'))

    if request.method == 'POST':
        entered_code = request.form.get('verification_code')

        if entered_code == pending['verification_code']:
            new_user = User(
                username=pending['username'],
                email=pending['email'],
                verification_code=pending['verification_code']
            )
            new_user.set_password(pending['password'])

            db.session.add(new_user)
            db.session.commit()
            session.pop('pending_user', None)

            flash('Verification successful. Please login.')
            return redirect(url_for('login'))
        else:
            flash('Invalid verification code')

    return render_template('verify_code.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if not user or not user.check_password(password):
            flash('Invalid username or password')
            return redirect(url_for('login'))
        
        login_user(user)
        return redirect(url_for('dashboard'))
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    global last_cache_update, price_cache

    # Get user transactions
    transactions = Transaction.query.filter_by(user_id=current_user.id).all()

    # Calculate current portfolio
    portfolio = {}
    sector_distribution = {}  # For sector pie chart
    portfolio_by_type = {'STOCK': 0, 'FUND': 0}  # For distribution by type

    # Track all tickers that have ever been in the portfolio
    all_tickers = set()
    
    for transaction in transactions:
        ticker = transaction.ticker.upper()
        all_tickers.add(ticker)
        
        if ticker not in portfolio:
            portfolio[ticker] = 0
        
        if transaction.transaction_type == 'buy':
            portfolio[ticker] += transaction.quantity
        else:  # sell
            portfolio[ticker] -= transaction.quantity

    # Check if we need to update prices
    if last_cache_update is None or (datetime.now() - last_cache_update) > timedelta(minutes=30):
        try:
            print("Fetching new stock data from Yahoo Finance...")
            # Call the preload function to update all prices
            preload_all_prices()
        except Exception as e:
            print(f"Error updating prices: {e}")
            # Continue with whatever data we have

    print('done getting stock info')

    # Process all positions (both open and closed)
    portfolio_data = []
    total_value = 0
    total_unrealized_pl = 0
    total_realized_pl = 0

    for ticker in all_tickers:
        try:
            # Get current shares (0 for closed positions)
            shares = portfolio.get(ticker, 0)
            
            # Get current price and other info
            price_info = get_latest_price_info(ticker)
            current_price = price_info['price']
            sector = price_info['sector']
            asset_type = next((t.asset_type for t in transactions if t.ticker.upper() == ticker and t.asset_type), 'unkown')
            daily_change_pct = price_info['daily_change_pct']
                        
            if current_price is None or current_price == 0:
                current_price = 0
                print(f"Warning: No price available for {ticker}")
            
            # Current position value (0 for closed positions)
            value = shares * current_price
            
            # Calculate profit/loss
            buy_transactions = [t for t in transactions
                            if t.ticker.upper() == ticker and t.transaction_type == 'buy']
            sell_transactions = [t for t in transactions
                                if t.ticker.upper() == ticker and t.transaction_type == 'sell']

            total_bought = sum([t.quantity for t in buy_transactions])
            total_sold = sum([t.quantity for t in sell_transactions])

            # Calculate weighted average cost basis
            if total_bought > 0:
                avg_cost = sum([t.price * t.quantity for t in buy_transactions]) / total_bought
                unrealized_pl = (current_price - avg_cost) * shares
                unrealized_pl_pct = (current_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
            else:
                avg_cost = 0
                unrealized_pl = 0
                unrealized_pl_pct = 0
        
            # Calculate realized profit/loss for closed positions
            realized_pl = 0
            if sell_transactions:
                for sell in sell_transactions:
                    # Calculate the cost basis at the time of sale using the average cost
                    sell_cost_basis = sell.quantity * avg_cost
                    # Calculate profit/loss for this sale
                    sell_revenue = sell.quantity * sell.price
                    realized_pl += sell_revenue - sell_cost_basis
        
            # Calculate realized profit/loss percentage based on the cost basis of sold shares
            sold_cost_basis = avg_cost * total_sold if avg_cost > 0 else 0
            realized_pl_pct = (realized_pl / sold_cost_basis * 100) if sold_cost_basis > 0 else 0
        
            # Add this position's P/L to totals (only count unrealized for open positions)
            if shares > 0:
                if sector not in sector_distribution:
                    sector_distribution[sector] = 0

                sector_distribution[sector] += value
                            
                total_unrealized_pl += unrealized_pl
                total_value += value
                
                print(asset_type)
                # Update portfolio by type
                if asset_type == 'fund':
                    portfolio_by_type['FUND'] += value
                else:
                    portfolio_by_type['STOCK'] += value
                
            # Always add realized P/L
            total_realized_pl += realized_pl
            
            # Determine position status
            position_status = "OPEN" if shares > 0 else "CLOSED"
            
            # Create position data
            position_data = {
                'ticker': ticker,
                'shares': shares,
                'current_price': current_price,
                'daily_change_pct': daily_change_pct,
                'value': value,
                'avg_cost': avg_cost,
                'sector': sector,
                'asset_type': asset_type,
                'unrealized_pl': unrealized_pl,
                'unrealized_pl_pct': unrealized_pl_pct,
                'realized_pl': realized_pl,
                'realized_pl_pct': realized_pl_pct,
                'total_bought': total_bought,
                'total_sold': total_sold,
                'status': position_status
            }

            # Only add positions that have actual transactions
            if total_bought > 0 or total_sold > 0:
                portfolio_data.append(position_data)
        
        except Exception as e:
            print(f"Error processing position {ticker}: {e}")
            import traceback
            traceback.print_exc()

    portfolio_data = sorted(portfolio_data, key=lambda x: x['value'], reverse=True)

    sector_data = []
    for sector, value in sector_distribution.items():
        if sector == 'None' or sector is None:
            sector = 'Unknown'
        sector_data.append({'sector': sector, 'value': value})

    
    # Calculate total P/L
    total_pl = total_unrealized_pl + total_realized_pl
    
    # Time series charts (keep the existing logic)
    profit_time_series = _generate_profit_time_series(transactions)
    avg_cost_time_series = _calculate_avg_cost_time_series(transactions)

    sector_data = sorted(sector_data, key=lambda x: x['value'], reverse=True)

    print(sector_data)
    print(portfolio_by_type)
    
    return render_template('dashboard.html',
                        portfolio=portfolio_data,
                        total_value=total_value,
                        total_unrealized_pl=total_unrealized_pl,
                        total_realized_pl=total_realized_pl,
                        total_pl=total_pl,
                        profit_time_series=profit_time_series,
                        avg_cost_time_series=avg_cost_time_series,
                        sector_data=sector_data,  # New data for sector pie chart
                        portfolio_by_type=portfolio_by_type,  # New data for type distribution
                        transactions=transactions)

def _generate_profit_time_series(transactions):
    # Extract the time series generation logic into a separate function
    sorted_transactions = sorted(transactions, key=lambda t: t.date)
    
    if not sorted_transactions:
        return []

    # Get date range from earliest transaction to today
    start_date = sorted_transactions[0].date
    
    # Create time series data structure
    profit_time_series = []
    running_unrealized_pl = 0
    running_realized_pl = 0
    
    # Create a portfolio snapshot for each date with transactions
    current_holdings = {}  # Track shares and cost basis by ticker
    
    for transaction in sorted_transactions:
        transaction_date = transaction.date.strftime('%Y-%m-%d')
        ticker = transaction.ticker.upper()
        
        # Initialize ticker in holdings if not present
        if ticker not in current_holdings:
            current_holdings[ticker] = {'shares': 0, 'cost_basis': 0, 'total_cost': 0}
        
        # Update holdings based on transaction
        if transaction.transaction_type == 'buy':
            # Update cost basis calculation
            new_shares = transaction.quantity
            new_cost = new_shares * transaction.price
            
            current_holdings[ticker]['shares'] += new_shares
            current_holdings[ticker]['total_cost'] += new_cost
            
            if current_holdings[ticker]['shares'] > 0:
                current_holdings[ticker]['cost_basis'] = current_holdings[ticker]['total_cost'] / current_holdings[ticker]['shares']
        else:  # sell
            # Calculate realized P/L for this sale
            sell_shares = transaction.quantity
            sell_price = transaction.price
            cost_basis = current_holdings[ticker]['cost_basis']
            
            # Realized P/L from this sale
            sale_realized_pl = (sell_price - cost_basis) * sell_shares
            running_realized_pl += sale_realized_pl
            
            # Update holdings
            current_holdings[ticker]['shares'] -= sell_shares
            
            # Maintain cost basis, only reduce total cost
            if current_holdings[ticker]['shares'] > 0:
                current_holdings[ticker]['total_cost'] = current_holdings[ticker]['shares'] * cost_basis
            else:
                current_holdings[ticker]['total_cost'] = 0
        
        # Calculate unrealized P/L based on current holdings
        running_unrealized_pl = 0

        try:            
            for ticker, holding in current_holdings.items():
                if holding['shares'] > 0:
                    # Try to get historical price on the transaction date
                    current_price = get_price_on_date(ticker, transaction.date)
                    
                    # Calculate unrealized P/L
                    ticker_unrealized_pl = (current_price - holding['cost_basis']) * holding['shares']
                    running_unrealized_pl += ticker_unrealized_pl
        except Exception as e:
            print(f"Error calculating unrealized P/L: {e}")
        
        # Add data point to time series
        profit_time_series.append({
            'date': transaction_date,
            'realized_pl': running_realized_pl,
            'unrealized_pl': running_unrealized_pl,
            'total_pl': running_realized_pl + running_unrealized_pl
        })

    return profit_time_series

@app.route('/add_transaction', methods=['GET', 'POST'])
@login_required
def add_transaction():
    if request.method == 'POST':
        ticker = request.form.get('ticker').upper()
        name = request.form.get('name').upper()
        quantity = float(request.form.get('quantity'))
        price = float(request.form.get('price'))
        transaction_type = request.form.get('transaction_type')
        date_str = request.form.get('date')
        
        date = datetime.strptime(date_str, '%Y-%m-%d') if date_str else datetime.now()
        
        # Validate transaction
        if transaction_type == 'sell':
            # Check if user has enough shares to sell
            portfolio = {}
            transactions = Transaction.query.filter_by(user_id=current_user.id).all()
            
            for t in transactions:
                if t.ticker.upper() == ticker:
                    if t.transaction_type == 'buy':
                        portfolio[ticker] = portfolio.get(ticker, 0) + t.quantity
                    else:
                        portfolio[ticker] = portfolio.get(ticker, 0) - t.quantity
            
            if ticker not in portfolio or portfolio[ticker] < quantity:
                flash(f'Not enough shares of {ticker} to sell')
                return redirect(url_for('add_transaction'))
        
        transaction = Transaction(
            user_id=current_user.id,
            ticker=ticker,
            name=name,
            quantity=quantity,
            price=price,
            date=date,
            transaction_type=transaction_type
        )
        
        db.session.add(transaction)
        db.session.commit()
        
        flash('Transaction added successfully')
        return redirect(url_for('dashboard'))
    
    return render_template('add_transaction.html')

def _calculate_avg_cost_time_series(transactions):
    sorted_transactions = sorted(transactions, key=lambda t: t.date)

    if not sorted_transactions:
        return {}

    ticker_data = {}
    
    for transaction in sorted_transactions:
        date = transaction.date.strftime('%Y-%m-%d')
        ticker = transaction.ticker.upper()
        
        if ticker not in ticker_data:
            ticker_data[ticker] = {
                'shares': 0,
                'total_cost': 0,
                'avg_cost': 0,
                'history': [],
                'initial_avg_cost': None  # NEW
            }

        data = ticker_data[ticker]

        if transaction.transaction_type == 'buy':
            new_shares = transaction.quantity
            new_cost = new_shares * transaction.price
            data['shares'] += new_shares
            data['total_cost'] += new_cost

            if data['shares'] > 0:
                data['avg_cost'] = data['total_cost'] / data['shares']

                if data['initial_avg_cost'] is None:
                    data['initial_avg_cost'] = data['avg_cost']

        elif transaction.transaction_type == 'sell':
            if data['shares'] > 0:
                data['shares'] -= transaction.quantity
                if data['shares'] > 0:
                    data['total_cost'] = data['avg_cost'] * data['shares']
                else:
                    data['total_cost'] = 0
                    data['avg_cost'] = 0

        # Calculate % change
        if data['initial_avg_cost'] and data['initial_avg_cost'] > 0:
            pct_change = (data['avg_cost'] - data['initial_avg_cost']) / data['initial_avg_cost'] * 100
        else:
            pct_change = 0

        data['history'].append({
            'date': date,
            'avg_cost': data['avg_cost'],
            'shares': data['shares'],
            'pct_change': pct_change  # NEW
        })

    avg_cost_series = {
        ticker: data['history']
        for ticker, data in ticker_data.items()
        if data['shares'] > 0
    }

    return avg_cost_series

@app.route('/delete_transaction/<int:transaction_id>', methods=['POST'])
@login_required
def delete_transaction(transaction_id):
    transaction = Transaction.query.get_or_404(transaction_id)
    
    if transaction.user_id != current_user.id:
        flash('Unauthorized')
        return redirect(url_for('dashboard'))
    
    db.session.delete(transaction)
    db.session.commit()
    
    flash('Transaction deleted')
    return redirect(url_for('dashboard'))

@app.route('/delete_all_transactions', methods=['POST'])
@login_required
def delete_all_transactions():
    try:
        # Delete all transactions for the current user
        Transaction.query.filter_by(user_id=current_user.id).delete()
        
        # Commit the deletion
        db.session.commit()
        
        flash('All transactions have been deleted successfully.')
        return redirect(url_for('dashboard'))
    
    except Exception as e:
        # Rollback in case of any error
        db.session.rollback()
        
        # Log the error (you might want to use a proper logging mechanism)
        print(f"Error deleting transactions: {e}")
        
        flash('An error occurred while deleting transactions.')
        return redirect(url_for('dashboard'))

@app.route('/download_transactions')
@login_required
def download_transactions():
    # Get all transactions for current user
    transactions = Transaction.query.filter_by(user_id=current_user.id).all()

    # Create a CSV in memory
    csv_data = io.StringIO()
    csv_writer = csv.writer(csv_data, delimiter = ';')
    
    # Write header row
    csv_writer.writerow(['date', 'ticker', 'name', 'action', 'quantity', 'price', 'amount'])
    
    # Write transaction data
    for transaction in transactions:
        # Force the quantity with a dot as the decimal separator
        total = f"{transaction.quantity * transaction.price:.2f}".replace(',', '.')

        quantity_str = f"{float(transaction.quantity):.2f}"

        csv_writer.writerow([
            transaction.date.strftime('%Y-%m-%d'),
            transaction.ticker,
            transaction.name,
            transaction.transaction_type,
            quantity_str,
            f"{transaction.price:.2f}".replace(',', '.'),
            total
        ])
    
    # Reset the pointer to the beginning of the StringIO object
    csv_data.seek(0)
    
    # Create filename with timestamp
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    filename = f"transactions_{timestamp}.csv"
    
    return send_file(
        io.BytesIO(csv_data.getvalue().encode('utf-8')),
        as_attachment=True,
        download_name=filename,
        mimetype='text/csv'
    )

def parse_float(value):
    value = value.strip()
    if ',' in value and '.' in value:
        value = value.replace(',', '')  # assumes , is thousands separator
    elif ',' in value:
        value = value.replace(',', '.')
    return float(value)

# Helper function to detect delimiter
def detect_delimiter(sample_line):
    """Detect delimiter by checking common delimiters."""
    delimiters = [',', ';', '\t', '|']
    max_delim = max(delimiters, key=lambda d: sample_line.count(d))
    return max_delim

@app.route('/import_csv', methods=['GET', 'POST'])
@login_required
def import_csv():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)

        file = request.files['file']

        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)

        if file and file.filename.endswith('.csv'):
            raw_data = file.stream.read().decode("UTF8")
            stream = io.StringIO(raw_data, newline=None)

            first_line = raw_data.splitlines()[0]
            delimiter = detect_delimiter(first_line)

            stream.seek(0)
            csv_reader = csv.DictReader(stream, delimiter=delimiter)

            transaction_count = 0
            error_count = 0
            new_tickers = set()

            for row in csv_reader:
                try:
                    try:
                        date = datetime.strptime(row['date'].strip(), '%d-%m-%Y')
                    except ValueError:
                        try:
                            date = datetime.strptime(row['date'].strip(), '%Y-%m-%d')
                        except ValueError:
                            date = datetime.strptime(row['date'].strip(), '%m/%d/%Y')

                    ticker = row.get('ticker', '').strip().upper()
                    name = row.get('name', '').strip()
                    asset_type = row.get('type', '').strip()

                    quantity = parse_float(row.get('quantity', '0'))
                    transaction_type = 'buy' if quantity > 0 else 'sell'

                    if 'action' in row and row['action'].strip().lower() in ['buy', 'sell']:
                        transaction_type = row['action'].strip().lower()

                    quantity = abs(quantity)
                    price = parse_float(row.get('price', '0'))

                    if price == 0 and 'amount' in row:
                        amount = abs(parse_float(row['amount']))
                        if quantity > 0:
                            price = amount / quantity

                    transaction = Transaction(
                        user_id=current_user.id,
                        name=name,
                        ticker=ticker,
                        quantity=quantity,
                        price=price,
                        date=date,
                        transaction_type=transaction_type,
                        asset_type=asset_type
                    )

                    db.session.add(transaction)
                    transaction_count += 1
                    new_tickers.add(ticker)

                except Exception as e:
                    error_count += 1
                    print(f"Error processing row: {row}, Error: {e}")

            # Commit all transactions
            db.session.commit()

            # Trigger price loading for newly imported tickers
            if new_tickers:
                preload_all_prices(selected_tickers=new_tickers)

            if error_count > 0:
                flash(f'Imported {transaction_count} transactions with {error_count} errors')
            else:
                flash(f'Successfully imported {transaction_count} transactions')

            return redirect(url_for('dashboard'))

    return render_template('import_csv.html')



# Add this route to download a CSV template
@app.route('/download_template')
@login_required
def download_template():
    template_data = io.StringIO()
    writer = csv.writer(template_data, delimiter = ';')
    
    # Write header row
    writer.writerow(['date', 'type', 'quantity', 'name', 'ticker', 'currency', 'amount', 'price', 'action', 'status'])
    
    # Write sample data row
    writer.writerow(['6-2-2024','stock', '0,826856', 'Adyen', 'ADYEN.AS', 'EUR', '999,6689', '1209', 'buy', 'closed'])
    
    template_data.seek(0)
    
    return send_file(
        io.BytesIO(template_data.getvalue().encode('utf-8')),
        as_attachment=True,
        download_name='transaction_template.csv',
        mimetype='text/csv',
    
    )

def preload_all_prices(selected_tickers=None):
    print("[Price Loader] Fetching prices...")

    try:
        # Determine tickers to load
        if selected_tickers is None:
            tickers = {t.ticker.upper() for t in Transaction.query.with_entities(Transaction.ticker).distinct()}
        else:
            tickers = {ticker.upper() for ticker in selected_tickers}

        if not tickers:
            print("No tickers to load prices for.")
            return

        print(f"Preloading prices for {len(tickers)} tickers: {', '.join(tickers)}")

        global price_cache, last_cache_update

        sector_cache = {}
        asset_type_cache = {}
        daily_change_cache = {}

        tickers_list = list(tickers)

        # Download historical data
        hist = yf.download(tickers_list, period='5d', interval='1d', group_by='ticker', session=cached_session)

        if hist.empty:
            print("No data returned from Yahoo Finance")
            return

        for ticker in tickers_list:
            try:
                # Support for single ticker (non-multi-index DataFrame)
                if isinstance(hist.columns, pd.Index) and ticker not in hist.columns:
                    print(f"No data for {ticker}")
                    continue
                elif isinstance(hist.columns, pd.MultiIndex) and ticker not in hist.columns.levels[0]:
                    print(f"No data for {ticker}")
                    continue

                ticker_hist = hist[ticker] if isinstance(hist.columns, pd.MultiIndex) else hist

                latest_close = ticker_hist['Close'].dropna().iloc[-1]
                if len(ticker_hist['Close'].dropna()) >= 2:
                    prev_close = ticker_hist['Close'].dropna().iloc[-2]
                    daily_change_pct = ((latest_close - prev_close) / prev_close) * 100
                else:
                    daily_change_pct = 0

                if not pd.isna(latest_close) and latest_close > 0:
                    price_cache[ticker] = latest_close
                    daily_change_cache[ticker] = daily_change_pct

                    try:
                        info = yf.Ticker(ticker).info

                        asset_type = 'STOCK'
                        if info.get('quoteType') in ['ETF', 'MUTUALFUND']:
                            asset_type = 'FUND'

                        sector = info.get('sector')
                        if sector is None and asset_type == 'FUND':
                            sector = info.get('categoryName', 'Index Fund')
                        elif sector is None:
                            sector = 'Unknown'

                        sector_cache[ticker] = sector
                        asset_type_cache[ticker] = asset_type

                    except Exception as e:
                        print(f"Could not fetch info for {ticker}: {e}")
                        sector_cache[ticker] = 'Unknown'
                        asset_type_cache[ticker] = 'STOCK'

                    current_date = datetime.now()

                    existing = StockPrice.query.filter(
                        StockPrice.ticker == ticker,
                        func.date(StockPrice.date) == current_date.date()
                    ).first()

                    if existing:
                        existing.price = latest_close
                        existing.date = current_date
                        existing.sector = sector_cache.get(ticker, 'Unknown')
                        existing.daily_change_pct = daily_change_pct
                    else:
                        new_price = StockPrice(
                            ticker=ticker,
                            price=latest_close,
                            date=current_date,
                            sector=sector_cache.get(ticker, 'Unknown'),
                            daily_change_pct=daily_change_pct
                        )
                        db.session.add(new_price)

                    print(f"✅ Stored price for {ticker}: {latest_close:.2f}")

                else:
                    print(f"⚠️ No valid price for {ticker}")

            except Exception as e:
                print(f"🔥 Error processing {ticker}: {e}")

        db.session.commit()
        last_cache_update = datetime.now()

    except Exception as e:
        print(f"💥 Error in preload_all_prices: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    with app.app_context():
        db.create_all()

    app.run(debug=True)