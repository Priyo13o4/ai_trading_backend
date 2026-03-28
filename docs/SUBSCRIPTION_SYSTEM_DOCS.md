# Subscription & Pricing System

## Overview

This is a complete subscription management system integrated with your AI Trading Bot, based on the `supabase_migration_v3.sql` schema. It includes subscription plans, user subscriptions with TTL (Time-To-Live), payment tracking, and a beautiful pricing page.

## Features

✅ **3-Tier Subscription System**
- Free Trial (3 days, all features)
- Basic Plan ($4.99/month, 1 trading pair)
- Premium Plan ($14.99/month, all pairs + premium features)

✅ **User Experience**
- Beautiful, responsive pricing page with gradient design
- Mobile-optimized navigation with pricing link
- Real-time subscription status in profile
- Trial countdown and expiration warnings
- Easy upgrade/downgrade flows

✅ **Backend Integration**
- Supabase database functions for subscription management
- Row-Level Security (RLS) for data protection
- Auto-expiration handling with TTL
- Payment history tracking
- Audit trail for all transactions

## File Structure

```
src/
├── components/
│   ├── subscription/
│   │   └── SubscriptionStatus.tsx    # Subscription status card for profile
│   └── marketing/
│       └── Navbar.tsx                 # Updated with Pricing link
├── hooks/
│   └── useSubscription.tsx            # Custom hook for subscription management
├── pages/
│   └── Pricing.tsx                    # Main pricing page
├── services/
│   └── subscriptionService.ts         # API service for subscriptions
├── types/
│   └── subscription.ts                # TypeScript types
└── App.tsx                            # Updated with /pricing route
```

## Database Schema (Already Deployed)

The subscription system uses these tables from `supabase_migration_v3.sql`:

1. **subscription_plans** - Available subscription plans
2. **user_subscriptions** - User subscription records with TTL
3. **payment_history** - Payment audit trail
4. **profiles** - User profiles (linked to auth.users)

Key database functions:
- `get_active_subscription(user_id)` - Get user's current subscription
- `can_access_pair(user_id, pair)` - Check trading pair access
- `create_subscription()` - Create new subscription
- `cancel_subscription()` - Cancel subscription
- `renew_subscription()` - Renew subscription
- `expire_subscriptions()` - Auto-expire TTL subscriptions (run via cron)

## Usage

### 1. Accessing the Pricing Page

Users can navigate to `/pricing` from:
- Navbar "Pricing" link
- Direct URL: `https://yourapp.com/pricing`

### 2. Viewing Subscription Status

In your Profile page, import and use the SubscriptionStatus component:

```tsx
import { SubscriptionStatus } from '@/components/subscription/SubscriptionStatus';

// Inside your Profile component
<SubscriptionStatus />
```

### 3. Using the Subscription Hook

```tsx
import { useSubscription } from '@/hooks/useSubscription';

function MyComponent() {
  const {
    currentSubscription,
    hasActiveSubscription,
    isOnTrial,
    daysRemaining,
    isExpiringSoon,
    canAccessPair,
    subscribe,
    cancelSubscription,
  } = useSubscription();

  // Check if user can access XAUUSD
  const hasAccess = await canAccessPair('XAUUSD');

  // Subscribe to a plan
  await subscribe(planId, {
    paymentProvider: 'stripe',
    externalId: 'stripe_sub_123',
  });

  // Cancel subscription
  await cancelSubscription(false); // false = cancel at period end
}
```

### 4. Checking Access in Protected Routes

```tsx
import { useSubscription } from '@/hooks/useSubscription';

function SignalPage() {
  const { canAccessPair } = useSubscription();
  const [hasAccess, setHasAccess] = useState(false);

  useEffect(() => {
    const checkAccess = async () => {
      const access = await canAccessPair('XAUUSD');
      setHasAccess(access);
    };
    checkAccess();
  }, []);

  if (!hasAccess) {
    return <UpgradePrompt />;
  }

  return <SignalData />;
}
```

## Integration Steps

### Step 1: Payment Gateway Integration (TODO)

The pricing page currently shows a placeholder for payment processing. To integrate:

**For Stripe:**

```tsx
// Install Stripe
npm install @stripe/stripe-js

// Create stripe service
import { loadStripe } from '@stripe/stripe-js';

const stripePromise = loadStripe(process.env.VITE_STRIPE_PUBLIC_KEY);

const handleSubscribe = async (plan: SubscriptionPlan) => {
  const stripe = await stripePromise;
  
  // Call your backend to create a Stripe checkout session
  const response = await fetch('/api/create-checkout-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      planId: plan.stripe_price_id,
      userId: user.id,
    }),
  });
  
  const { sessionId } = await response.json();
  
  // Redirect to Stripe checkout
  await stripe.redirectToCheckout({ sessionId });
};
```

**For Razorpay (Indian users):**

```tsx
// Install Razorpay
npm install razorpay

// Frontend integration
const handleSubscribe = async (plan: SubscriptionPlan) => {
  const options = {
    key: process.env.VITE_RAZORPAY_KEY_ID,
    amount: plan.price_usd * 100, // Razorpay expects paise
    currency: 'INR',
    name: 'PipFactor',
    description: plan.display_name,
    handler: async (response) => {
      // Verify payment and create subscription
      await subscriptionService.createSubscription(user.id, plan.id, {
        paymentProvider: 'razorpay',
        externalId: response.razorpay_payment_id,
      });
    },
  };
  
  const razorpay = new window.Razorpay(options);
  razorpay.open();
};
```

### Step 2: Webhook Handler (Backend)

Create an API endpoint to handle payment webhooks:

```python
# FastAPI example
@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            # Create subscription in database
            await create_subscription_from_payment(session)
        
        elif event['type'] == 'invoice.payment_succeeded':
            # Renew subscription
            await renew_subscription_from_payment(event['data']['object'])
        
        elif event['type'] == 'customer.subscription.deleted':
            # Cancel subscription
            await cancel_subscription_from_event(event['data']['object'])
        
        return {"status": "success"}
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
```

### Step 3: Cron Job for Auto-Expiration

Set up a cron job to run the `expire_subscriptions()` function daily:

**Using Supabase Cron:**

```sql
-- Run daily at midnight UTC
SELECT cron.schedule(
    'expire-subscriptions',
    '0 0 * * *',
    $$
    SELECT expire_subscriptions();
    $$
);
```

**Using n8n (already in your stack):**

1. Create a workflow that triggers daily
2. Execute SQL: `SELECT expire_subscriptions();`
3. Send email notifications to users whose subscriptions expired

## Environment Variables

Add to `.env`:

```env
# Stripe (if using Stripe)
VITE_STRIPE_PUBLIC_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Razorpay (if using Razorpay)
VITE_RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
```

## Styling & Customization

### Color Scheme

The pricing page uses your existing brand colors:
- Gold accent: `#D4AF37` (buttons, badges, highlights)
- Dark blue: Slate-900 to Blue-900 gradient
- White text: Slate-100 for readability

### Customizing Plans

To modify subscription plans, update the database:

```sql
-- Update plan price
UPDATE subscription_plans
SET price_usd = 9.99
WHERE name = 'basic';

-- Add new plan
INSERT INTO subscription_plans (
    name, display_name, description,
    price_usd, billing_period, pairs_allowed
) VALUES (
    'enterprise', 'Enterprise', 'Custom solutions',
    49.99, 'monthly', ARRAY['XAUUSD', 'EURUSD', 'GBPUSD']
);
```

### Customizing Features Display

Edit `getPlanFeatures()` in `Pricing.tsx` to change how features are displayed.

## Testing

### Test Subscription Flow

1. **Free Trial:**
   ```tsx
   // User signs up -> Auto-creates 3-day trial
   // Check: SELECT * FROM user_subscriptions WHERE user_id = 'xxx';
   ```

2. **Upgrade to Paid:**
   ```tsx
   await subscriptionService.createSubscription(userId, basicPlanId, {
     paymentProvider: 'manual', // For testing
   });
   ```

3. **Check Access:**
   ```tsx
   const hasAccess = await subscriptionService.canAccessPair(userId, 'XAUUSD');
   console.log('Has access:', hasAccess);
   ```

4. **Expire Subscription:**
   ```sql
   -- Manually expire for testing
   UPDATE user_subscriptions
   SET expires_at = NOW() - INTERVAL '1 day'
   WHERE user_id = 'xxx';
   
   -- Run expiration function
   SELECT expire_subscriptions();
   ```

## Security Considerations

✅ Row-Level Security (RLS) enabled on all tables
✅ Service role required for subscription management
✅ User can only view their own data
✅ Payment verification required before subscription creation
✅ Webhook signature validation (when integrated)

## Common Issues & Solutions

### Issue: Subscription not showing on pricing page

**Solution:**
```sql
-- Check if subscription exists
SELECT * FROM get_active_subscription('user-uuid');

-- Verify RLS policies
SELECT * FROM user_subscriptions WHERE user_id = auth.uid();
```

### Issue: Can't access trading pair

**Solution:**
```sql
-- Check pair access
SELECT can_access_pair('user-uuid', 'XAUUSD');

-- Verify subscription is current
SELECT * FROM get_active_subscription('user-uuid')
WHERE is_current = true;
```

### Issue: Trial not created on signup

**Solution:**
```sql
-- Check trigger is active
SELECT * FROM pg_trigger
WHERE tgname = 'on_auth_user_created';

-- Manually create trial
SELECT create_subscription(
    'user-uuid',
    (SELECT id FROM subscription_plans WHERE name = 'free'),
    'manual',
    NULL,
    3,
    '{}'::jsonb
);
```

## Next Steps

1. ✅ Pricing page created
2. ✅ Navigation updated
3. ✅ Subscription hook implemented
4. ✅ TypeScript types defined
5. ⏳ **TODO:** Integrate payment gateway (Stripe/Razorpay)
6. ⏳ **TODO:** Set up webhook handlers
7. ⏳ **TODO:** Configure cron job for expiration
8. ⏳ **TODO:** Add email notifications
9. ⏳ **TODO:** Add subscription analytics dashboard

## Support

For questions or issues:
- Check database with: `SELECT * FROM active_subscriptions_view;`
- Review logs in Supabase Dashboard
- Test functions manually in SQL Editor

---

**Built with:** React, TypeScript, Tailwind CSS, shadcn/ui, Supabase
**Schema:** Based on `supabase_migration_v3.sql`
**Status:** ✅ Frontend Complete | ⏳ Payment Integration Pending
