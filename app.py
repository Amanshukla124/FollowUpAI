import os
import stripe
from datetime import datetime
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── App setup ───────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY']                  = os.getenv('SECRET_KEY', 'dev-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI']     = 'sqlite:///followupai.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db           = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view         = 'login'
login_manager.login_message      = 'Please sign in to generate emails.'
login_manager.login_message_category = 'info'

# ── Stripe ──────────────────────────────────────────────────────────────────
stripe.api_key          = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY  = os.getenv('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET   = os.getenv('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRO_PRICE_ID     = os.getenv('STRIPE_PRO_PRICE_ID', '')

# ── OpenRouter ──────────────────────────────────────────────────────────────
openai_client = OpenAI(
    api_key=os.getenv('OPENROUTER_API_KEY',
                      ''),
    base_url="https://openrouter.ai/api/v1"
)

FREE_MONTHLY_LIMIT = 3   # emails per month on the free plan


# ══════════════════════════════════════════════════════════════════════════════
#  MODEL
# ══════════════════════════════════════════════════════════════════════════════
class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id                     = db.Column(db.Integer,     primary_key=True)
    name                   = db.Column(db.String(80),  nullable=False)
    email                  = db.Column(db.String(120), unique=True, nullable=False)
    password_hash          = db.Column(db.String(200), nullable=False)
    plan                   = db.Column(db.String(20),  default='free')   # 'free' | 'pro'
    stripe_customer_id     = db.Column(db.String(100), nullable=True)
    stripe_subscription_id = db.Column(db.String(100), nullable=True)
    monthly_count          = db.Column(db.Integer,     default=0)
    last_reset_month       = db.Column(db.String(7),   default='')       # 'YYYY-MM'
    created_at             = db.Column(db.DateTime,    default=datetime.utcnow)

    # ── helpers ────────────────────────────────────────────────────────────
    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def refresh_monthly_count(self):
        """Reset counter when a new calendar month begins."""
        month = datetime.utcnow().strftime('%Y-%m')
        if self.last_reset_month != month:
            self.monthly_count    = 0
            self.last_reset_month = month
            db.session.commit()

    @property
    def is_pro(self):
        return self.plan == 'pro'

    @property
    def emails_remaining(self):
        if self.is_pro:
            return None           # unlimited
        self.refresh_monthly_count()
        return max(0, FREE_MONTHLY_LIMIT - self.monthly_count)

    @property
    def can_generate(self):
        if self.is_pro:
            return True
        self.refresh_monthly_count()
        return self.monthly_count < FREE_MONTHLY_LIMIT


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def landing():
    return render_template('landing.html')


@app.route('/pricing')
def pricing():
    return render_template(
        'pricing.html',
        stripe_pk=STRIPE_PUBLISHABLE_KEY,
        free_limit=FREE_MONTHLY_LIMIT,
    )


@app.route('/templates')
def email_templates():
    return render_template('templates.html')


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == 'POST':
        name  = request.form.get('name',     '').strip()
        email = request.form.get('email',    '').strip().lower()
        pw    = request.form.get('password', '')

        if not name or not email or not pw:
            flash('All fields are required.', 'error')
            return redirect(url_for('signup'))
        if len(pw) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return redirect(url_for('signup'))
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists. Try signing in.', 'error')
            return redirect(url_for('signup'))

        user = User(name=name, email=email)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash(f"Welcome, {name}! You're on the Free plan — {FREE_MONTHLY_LIMIT} emails/month.", 'success')
        return redirect(url_for('home'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == 'POST':
        email = request.form.get('email',    '').strip().lower()
        pw    = request.form.get('password', '')
        user  = User.query.filter_by(email=email).first()

        if user and user.check_password(pw):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('home'))

        flash('Invalid email or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('landing'))


# ══════════════════════════════════════════════════════════════════════════════
#  APP ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/app')
@login_required
def home():
    current_user.refresh_monthly_count()
    return render_template('index.html',
                           user=current_user,
                           free_limit=FREE_MONTHLY_LIMIT)


@app.route('/account')
@login_required
def account():
    current_user.refresh_monthly_count()
    upgraded = request.args.get('upgraded') == '1'
    return render_template('account.html',
                           user=current_user,
                           free_limit=FREE_MONTHLY_LIMIT,
                           upgraded=upgraded)


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    # ── Enforce monthly limit ────────────────────────────────────────────────
    if not current_user.can_generate:
        flash(
            f"You've used all {FREE_MONTHLY_LIMIT} free emails this month. "
            "Upgrade to Pro for unlimited emails. ✨",
            'upgrade'
        )
        return redirect(url_for('pricing'))

    transcript = request.form.get('transcript', '')
    tone       = request.form.get('tone',       'formal')
    mode       = request.form.get('mode',       'followup')

    if mode == 'cold':
        prompt = f"""Write a compelling cold outreach email.

Tone: {tone}

Context / Goal:
{transcript}

Include:
- A personalized subject line suggestion
- A strong opening hook
- Clear value proposition
- A specific call-to-action
- Professional sign-off"""
    else:
        prompt = f"""Convert this meeting transcript into a professional follow-up email.

Tone: {tone}

Include:
- Appropriate greeting
- Summary of discussion
- Action items with owners
- Next steps and timeline

Transcript:
{transcript}"""

    response = openai_client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    email = response.choices[0].message.content

    # ── Increment usage for free users ───────────────────────────────────────
    if not current_user.is_pro:
        current_user.monthly_count += 1
        db.session.commit()

    return render_template('result.html', email=email, user=current_user)


# ══════════════════════════════════════════════════════════════════════════════
#  STRIPE ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        # Create or reuse a Stripe Customer
        if not current_user.stripe_customer_id:
            customer = stripe.Customer.create(
                email=current_user.email,
                name=current_user.name,
                metadata={'user_id': current_user.id}
            )
            current_user.stripe_customer_id = customer.id
            db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            customer=current_user.stripe_customer_id,
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRO_PRICE_ID, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('account', _external=True) + '?upgraded=1',
            cancel_url=url_for('pricing', _external=True),
        )
        return redirect(checkout_session.url, code=303)

    except Exception as e:
        flash('Something went wrong with checkout. Please try again.', 'error')
        return redirect(url_for('pricing'))


@app.route('/billing-portal', methods=['POST'])
@login_required
def billing_portal():
    try:
        portal = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=url_for('account', _external=True),
        )
        return redirect(portal.url, code=303)
    except Exception:
        flash('Could not open billing portal. Please try again.', 'error')
        return redirect(url_for('account'))


@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({'error': 'Invalid payload or signature'}), 400

    etype = event['type']
    obj   = event['data']['object']

    if etype == 'checkout.session.completed':
        customer_id = obj.get('customer')
        sub_id      = obj.get('subscription')
        user = User.query.filter_by(stripe_customer_id=customer_id).first()
        if user:
            user.plan                   = 'pro'
            user.stripe_subscription_id = sub_id
            db.session.commit()

    elif etype == 'customer.subscription.deleted':
        sub_id = obj.get('id')
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            user.plan                   = 'free'
            user.stripe_subscription_id = None
            db.session.commit()

    elif etype == 'invoice.payment_failed':
        pass   # future: notify user by email

    return jsonify({'status': 'ok'}), 200


# ══════════════════════════════════════════════════════════════════════════════
#  INIT DB & RUN
# ══════════════════════════════════════════════════════════════════════════════
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
