import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone, timedelta
import mysql.connector
import joblib
import numpy as np
import tensorflow as tf
import requests
from functools import wraps

WAQI_TOKEN = os.environ['WAQI_TOKEN']

# ── Load LSTM model and scalers ───────────────────────────────────────────
model = tf.keras.models.load_model('model.keras')
X_scaler = joblib.load('X_scaler.pkl')
y_scaler = joblib.load('y_scaler.pkl')
WINDOW_SIZE = 30

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

# ── Database connection ────────────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        user=os.getenv('DB_USER', 'root'),
        password=os.environ['DB_PASSWORD'],
        database=os.getenv('DB_NAME', 'airiq')
    )

# ── Login required decorator ───────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login first')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Home ───────────────────────────────────────────────────────────────────
@app.route('/')
def home():
    db = get_db()
    cursor = db.cursor()

    # Total submissions
    cursor.execute("SELECT COUNT(*) FROM submissions")
    total_submissions = cursor.fetchone()[0]

    # Total unique cities
    cursor.execute("SELECT COUNT(DISTINCT location) FROM submissions")
    total_cities = cursor.fetchone()[0]

    # Total registered users
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    # Latest AQI submitted
    cursor.execute("SELECT aqi_value, location FROM submissions ORDER BY submitted_at DESC LIMIT 1")
    latest = cursor.fetchone()
    latest_aqi = latest[0] if latest else 'N/A'
    latest_location = latest[1] if latest else 'N/A'

    db.close()

    return render_template('index.html',
                           total_submissions=total_submissions,
                           total_cities=total_cities,
                           total_users=total_users,
                           latest_aqi=latest_aqi,
                           latest_location=latest_location)

# ── Register ───────────────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email    = request.form['email']
        
        # Check whether passwords match
        if request.form['password'] != request.form['confirm_password']:
            flash('Passwords do not match.')
            return render_template('register.html')
        
        password = generate_password_hash(request.form['password'])

        db = get_db()
        cursor = db.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (username, email, password) VALUES (%s, %s, %s)",
                (username, email, password)
            )
            db.commit()
            flash('Account created! Please login.')
            return redirect(url_for('login'))
        except mysql.connector.errors.IntegrityError:
            flash('Email already exists.')
        finally:
            db.close()
           
    return render_template('register.html')

# ── Login ──────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form['email']
        password = request.form['password']

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        db.close()

        if user and check_password_hash(user[3], password):
            session['user_id']  = user[0]
            session['username'] = user[1]
            session['role']     = user[4]
            return redirect(url_for('dashboard'))
        else:
            flash('Wrong email or password.')

    return render_template('login.html')

# ── Logout ─────────────────────────────────────────────────────────────────
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


# ── Submit AQI reading ─────────────────────────────────────────────────────
@app.route('/submit', methods=['GET', 'POST'])
@login_required
def submit():
    if request.method == 'POST':
        location = request.form['location']

        try:
            aqi_value = int(request.form['aqi_value'])

            if not 0 <= aqi_value <= 500:
                flash('AQI value must be between 0 and 500.')
                return redirect(url_for('submit'))

        except (ValueError, TypeError):
            flash('Please enter a valid AQI value.')
            return redirect(url_for('submit'))

        user_id = session['user_id']

        # Calculate category on the server
        if aqi_value <= 50:
            category = 'Good'
        elif aqi_value <= 100:
            category = 'Moderate'
        elif aqi_value <= 150:
            category = 'Unhealthy for Sensitive Groups'
        elif aqi_value <= 200:
            category = 'Unhealthy'
        elif aqi_value <= 300:
            category = 'Very Unhealthy'
        else:
            category = 'Hazardous'

        ist = timezone(timedelta(hours=5, minutes=30))
        current_time = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S')

        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO submissions (user_id, location, aqi_value, category, submitted_at) VALUES (%s, %s, %s, %s, %s)",
            (user_id, location, aqi_value, category, current_time)
        )
        db.commit()
        db.close()

        flash('Reading submitted successfully!')
        return redirect(url_for('dashboard'))

    return render_template('submit.html')

# ── Dashboard ──────────────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """SELECT location, aqi_value, category, submitted_at 
        FROM submissions 
        ORDER BY submitted_at DESC 
        LIMIT 50"""
    )
    submissions = cursor.fetchall()
    db.close()

    labels = [row[0] for row in submissions]
    values = [row[1] for row in submissions]

    return render_template('dashboard.html',
                           submissions=submissions,
                           labels=labels,
                           values=values,
                           username=session['username'])

# ── Admin panel ────────────────────────────────────────────────────────────
@app.route('/admin')
@login_required
def admin():
    if session['role'] != 'admin':
        flash('Access denied.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
    SELECT s.id, u.username, s.location, s.aqi_value, s.category, s.submitted_at 
    FROM submissions s 
    JOIN users u ON s.user_id = u.id 
    ORDER BY s.submitted_at DESC
    """)
    all_submissions = cursor.fetchall()
    db.close()


    return render_template('admin.html', submissions=all_submissions)

# ── Prediction ────────────────────────────────────────────────────────────
@app.route('/predict', methods=['GET', 'POST'])
@login_required
def predict():
    prediction = None
    category = None
    error = None
    city = None
    current_aqi = None
    forecast_labels = []
    forecast_values = []
    history_labels = []
    history_values = []
    health_message = None
    health_tip = None

    if request.method == 'POST':
        try:
            city = request.form.get('city', '').strip()
            token = WAQI_TOKEN

            url = f"https://api.waqi.info/feed/{city}/?token={WAQI_TOKEN}"
            response = requests.get(url, timeout=10)
            data = response.json()

            if data['status'] != 'ok':
                error = f"City '{city}' not found. Try a different city name."
            else:
                current_aqi = data['data']['aqi']
                forecast_data = data['data']['forecast']['daily']

                # ── History (last 30 days from pm25) ──────────────────────
                pm25_history = forecast_data.get('pm25', [])
                history_labels = [entry['day'] for entry in pm25_history]
                history_values = [entry['avg'] for entry in pm25_history]

                # Pad to 30 if needed
                aqi_values = history_values.copy()
                while len(aqi_values) < 30:
                    aqi_values.insert(0, current_aqi)
                aqi_values = aqi_values[-30:]

                # ── LSTM prediction ───────────────────────────────────────
                aqi_array = np.array(aqi_values).reshape(-1, 1)
                scaled_input = X_scaler.transform(aqi_array).reshape(1, WINDOW_SIZE, 1)
                scaled_pred = model.predict(scaled_input)
                prediction = round(float(y_scaler.inverse_transform(scaled_pred)[0][0]), 2)

                # ── 7 day forecast using rolling predictions ──────────────
                rolling = aqi_values.copy()
                forecast_labels = []
                forecast_values = []

                for i in range(7):
                    inp = np.array(rolling[-30:]).reshape(-1, 1)
                    scaled_inp = X_scaler.transform(inp).reshape(1, WINDOW_SIZE, 1)
                    scaled_out = model.predict(scaled_inp, verbose=0)
                    next_val = round(float(y_scaler.inverse_transform(scaled_out)[0][0]), 2)
                    rolling.append(next_val)
                    forecast_values.append(next_val)
                    day = (datetime.now() + timedelta(days=i+1)).strftime('%b %d')
                    forecast_labels.append(day)

                # ── Category ──────────────────────────────────────────────
                if prediction <= 50:
                    category = "Good"
                elif prediction <= 100:
                    category = "Satisfactory"
                elif prediction <= 200:
                    category = "Moderate"
                elif prediction <= 300:
                    category = "Poor"
                elif prediction <= 400:
                    category = "Very Poor"
                else:
                    category = "Severe"

                # ── Health message based on predicted AQI ─────────────────
                aqi_val = prediction
                if aqi_val <= 50:
                    health_message = "Air quality is great — it's a perfect day to go outside! 🌿"
                    health_tip = "No precautions needed. Enjoy outdoor activities freely."
                elif aqi_val <= 100:
                    health_message = "Air quality is acceptable for most people. 😊"
                    health_tip = "Unusually sensitive individuals should consider limiting prolonged outdoor exertion."
                elif aqi_val <= 200:
                    health_message = "Sensitive groups may experience health effects. 😷"
                    health_tip = "People with respiratory or heart conditions, children and elderly should limit prolonged outdoor exertion."
                elif aqi_val <= 300:
                    health_message = "Everyone may begin to experience health effects. ⚠️"
                    health_tip = "Wear a mask outdoors. Avoid prolonged physical activity outside. Keep windows closed."
                elif aqi_val <= 400:
                    health_message = "Health alert — everyone may experience serious effects. 🚨"
                    health_tip = "Avoid all outdoor activity. Wear N95 mask if going out. Keep indoors with air purifier if possible."
                else:
                    health_message = "Emergency conditions — hazardous air quality! 🆘"
                    health_tip = "Stay indoors. Do not go outside under any circumstances. Seek medical attention if experiencing breathing difficulty."

        except Exception as e:
            error = f"Something went wrong: {str(e)}"

    return render_template('predict.html',
                           prediction=prediction,
                           category=category,
                           error=error,
                           city=city,
                           current_aqi=current_aqi,
                           forecast_labels=forecast_labels,
                           forecast_values=forecast_values,
                           history_labels=history_labels,
                           history_values=history_values,
                           health_message=health_message,
                           health_tip=health_tip,
                           window_size=WINDOW_SIZE)


# ── Delete route ──────────────────────────────────────────────────────────
@app.route('/delete/<int:submission_id>', methods=['POST'])
@login_required
def delete_submission(submission_id):
    if session['role'] != 'admin':
        flash('Access denied.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "DELETE FROM submissions WHERE id = %s",
        (submission_id,)
    )
    db.commit()
    db.close()

    flash('Submission deleted successfully.')
    return redirect(url_for('admin'))


# ── Always last line ───────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG') == '1')