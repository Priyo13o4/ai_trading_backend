# Email Verification System - Setup Guide

## 🎯 Overview

This hybrid email verification system works seamlessly across:
- ✅ **Desktop Web** (Chrome, Firefox, Safari, etc.)
- ✅ **Mobile Web** (iOS Safari, Android Chrome, etc.)  
- ✅ **Mobile App** (React Native/Expo - future ready)

## 📁 Files Created

### Core Hooks
1. **`src/hooks/useDeviceType.ts`** - Platform detection (desktop/mobile-web/mobile-app)
2. **`src/hooks/useVerification.ts`** - Email verification logic

### Components
3. **`src/pages/AuthCallback.tsx`** - Unified verification page for all platforms

### Utilities
4. **`src/utils/verification-utils.ts`** - Token validation, error handling, cross-tab sync
5. **`src/config/deep-link-config.ts`** - Deep link configuration for mobile app

### Types
6. **`src/types/window.d.ts`** - TypeScript definitions for window extensions

### Updated Files
7. **`src/App.tsx`** - Added `/auth/callback` route
8. **`src/lib/supabase.ts`** - Enhanced auth configuration
9. **`src/hooks/useAuth.tsx`** - Updated signup with redirect URL

---

## 🚀 Quick Start

### 1. Configure Supabase Dashboard

Go to **Supabase Dashboard → Authentication → URL Configuration**

Add these redirect URLs:

```
# Development
http://localhost:3000/auth/callback
http://localhost:3000/**

# Production
https://pipfactor.com/auth/callback
https://pipfactor.com/**

# Mobile App (for future)
pipfactor://auth/callback
```

### 2. Update Email Templates (Optional but Recommended)

Go to **Supabase Dashboard → Authentication → Email Templates**

**Confirm Signup Template:**
```html
<h2>Confirm your email</h2>
<p>Follow this link to confirm your email:</p>
<p><a href="{{ .ConfirmationURL }}">Confirm Email</a></p>

<!-- Mobile-friendly button -->
<a href="{{ .ConfirmationURL }}" 
   style="display: inline-block; padding: 12px 24px; background: #3b82f6; 
          color: white; text-decoration: none; border-radius: 6px;">
  Verify Email
</a>
```

### 3. Test the Flow

#### Desktop/Mobile Web Testing:

1. **Sign up a new user:**
   ```bash
   # Open your app
   http://localhost:3000
   
   # Click "Sign Up"
   # Enter email and password
   ```

2. **Check email inbox** (use a real email or Supabase's email testing)

3. **Click verification link** in email
   - Should redirect to: `http://localhost:3000/auth/callback?token=xxx&type=signup`

4. **AuthCallback page will:**
   - ✅ Extract token from URL
   - ✅ Verify with Supabase
   - ✅ Show success animation
   - ✅ Redirect to `/signal` after 1.5s
   - ✅ Clean token from URL (security)
   - ✅ Sync verification across browser tabs

#### Mobile App Testing (Future):

When you build the mobile app:

**iOS Simulator:**
```bash
xcrun simctl openurl booted "pipfactor://auth/callback?token=test123&type=signup"
```

**Android Emulator:**
```bash
adb shell am start -W -a android.intent.action.VIEW \
  -d "pipfactor://auth/callback?token=test123&type=signup" \
  com.pipfactor.app
```

---

## 🔧 Platform Detection

The system automatically detects which platform the user is on:

```typescript
import { useDeviceType } from '@/hooks/useDeviceType';

const { type, isMobile, isDesktop, isNativeApp } = useDeviceType();

// type: 'desktop' | 'mobile-web' | 'mobile-app'
// isMobile: boolean (true for mobile-web and mobile-app)
// isDesktop: boolean
// isNativeApp: boolean (true only for mobile app)
```

### Testing Different Platforms

Add `?platform=mobile-web` to URL for testing:

```
http://localhost:3000/auth/callback?token=xxx&platform=mobile-web
```

Options: `desktop`, `mobile-web`, `mobile-app`

---

## 📱 Mobile App Integration (Future)

### Step 1: Expo app.json

Add to `app.json`:

```json
{
  "expo": {
    "scheme": "pipfactor",
    "ios": {
      "bundleIdentifier": "com.pipfactor.app",
      "associatedDomains": ["applinks:pipfactor.com"]
    },
    "android": {
      "package": "com.pipfactor.app",
      "intentFilters": [
        {
          "action": "VIEW",
          "autoVerify": true,
          "data": [
            {
              "scheme": "https",
              "host": "pipfactor.com",
              "pathPrefix": "/auth"
            },
            {
              "scheme": "pipfactor"
            }
          ],
          "category": ["BROWSABLE", "DEFAULT"]
        }
      ]
    }
  }
}
```

### Step 2: Handle Deep Links in App

```typescript
// In your React Native app
import * as Linking from 'expo-linking';
import { useEffect } from 'react';

const App = () => {
  useEffect(() => {
    // Handle initial URL (app opened from deep link)
    Linking.getInitialURL().then((url) => {
      if (url) handleDeepLink(url);
    });

    // Handle URL when app is already open
    const subscription = Linking.addEventListener('url', ({ url }) => {
      handleDeepLink(url);
    });

    return () => subscription.remove();
  }, []);

  const handleDeepLink = (url: string) => {
    // Parse: pipfactor://auth/callback?token=xxx
    const { hostname, path, queryParams } = Linking.parse(url);
    
    if (hostname === 'auth' && path === 'callback') {
      // Navigate to verification screen in your app
      navigation.navigate('AuthCallback', { 
        token: queryParams.token,
        type: queryParams.type 
      });
    }
  };

  return <NavigationContainer>...</NavigationContainer>;
};
```

### Step 3: Apple App Site Association (AASA)

Host this file at: `https://pipfactor.com/.well-known/apple-app-site-association`

```json
{
  "applinks": {
    "apps": [],
    "details": [
      {
        "appID": "TEAM_ID.com.pipfactor.app",
        "paths": ["/auth/*"]
      }
    ]
  }
}
```

### Step 4: Android Asset Links

Host this file at: `https://pipfactor.com/.well-known/assetlinks.json`

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "package_name": "com.pipfactor.app",
      "sha256_cert_fingerprints": [
        "YOUR_SHA256_FINGERPRINT"
      ]
    }
  }
]
```

---

## 🔐 Security Features

### ✅ Token Validation
- Format validation before API call
- Expiry handling with clear error messages
- Invalid token detection

### ✅ URL Cleaning
- Removes tokens from browser history (prevents sharing)
- Uses `replaceState` to clean URL without reload

### ✅ Cross-Tab Sync
- Uses `localStorage` events
- Notifies all tabs when verification succeeds
- Prevents duplicate verification attempts

### ✅ Error Handling
- Expired token → "Request new link" button
- Invalid token → "Request new link" button
- Network error → "Retry" button
- Already verified → Auto-redirect

---

## 🎨 UI/UX Features

### Responsive Design
- **Desktop**: Full-screen centered card
- **Mobile**: Smaller card, optimized for touch
- **Tablet**: Medium-sized modal

### Loading States
1. **Extracting** - Reading verification link
2. **Verifying** - Confirming with server
3. **Success** - Email verified! (with animation)
4. **Error** - Clear error message with retry options

### Platform Indicators
- Shows "📱 Mobile App" or "💻 Desktop Browser" in UI
- Development mode shows detailed platform info

---

## 🧪 Testing Checklist

### Desktop Web
- [ ] Sign up new user
- [ ] Receive verification email
- [ ] Click link in email
- [ ] See verification page load
- [ ] See success animation
- [ ] Auto-redirect to /signal
- [ ] Token removed from URL
- [ ] Can log in after verification

### Mobile Web
- [ ] Same as desktop but on mobile browser
- [ ] UI responsive (smaller card)
- [ ] Touch-friendly buttons
- [ ] No horizontal scroll

### Cross-Tab Sync
- [ ] Open app in two tabs
- [ ] Verify email in tab 1
- [ ] Tab 2 should detect verification
- [ ] Tab 2 should redirect automatically

### Error Scenarios
- [ ] Expired link shows correct error
- [ ] Invalid link shows correct error
- [ ] "Resend" button works
- [ ] Network error shows retry option
- [ ] Already verified shows success

---

## 📊 Monitoring & Debugging

### Console Logs

All verification steps log to console with `[Verification]` prefix:

```
[Verification] Token extracted: { type: 'signup', hasError: false }
[Verification] Verifying token...
[Verification] Success! Session created: user_id_here
```

### Platform Detection Logs

```
[DeviceType] Test mode: mobile-web
[AuthCallback] Platform: desktop
[AuthCallback] Is native app: false
```

### Storage Keys

- **Session**: `pipfactor-auth` (localStorage)
- **Device Type**: `pipfactor_device_type` (sessionStorage)
- **Verification Sync**: `pipfactor_verification_success` (localStorage)

---

## 🔄 Migration from Old System

If you had a previous verification system:

### 1. Remove Old Routes
```typescript
// Remove these if they exist:
<Route path="/verify" element={<OldVerify />} />
<Route path="/confirm" element={<OldConfirm />} />
```

### 2. Update Redirect URLs in Supabase
- Remove old redirect URLs
- Add new `/auth/callback` URL

### 3. Update Email Templates
- Change links to point to `/auth/callback`

---

## 🐛 Troubleshooting

### "No token found in URL"
- Check Supabase redirect URL is `/auth/callback`
- Check email template uses `{{ .ConfirmationURL }}`
- Verify URL param is `token` not `access_token`

### "Invalid token format"
- Token might be corrupted
- Check URL encoding/decoding
- Request new verification email

### "Token expired"
- Tokens expire after 24 hours (Supabase default)
- Click "Send New Verification Email"
- Adjust expiry in Supabase dashboard if needed

### Verification succeeds but doesn't redirect
- Check console for errors
- Verify `/signal` route exists
- Check `redirectOnSuccess` prop in `useVerification`

### Mobile app deep link not working
- Verify `app.json` configuration
- Test with `adb` or `xcrun` commands
- Check app is installed and scheme is registered

---

## 📚 API Reference

### useVerification Hook

```typescript
const {
  status,        // 'idle' | 'extracting' | 'verifying' | 'success' | 'error'
  error,         // VerificationError | null
  isLoading,     // boolean
  verify,        // () => Promise<void>
  resend,        // () => Promise<void>
  canResend,     // boolean
  deviceInfo,    // DeviceInfo
} = useVerification({
  autoVerify: true,                // Auto-verify on mount
  redirectOnSuccess: '/signal',    // Where to go after success
  onSuccess: () => {},             // Success callback
  onError: (err) => {},            // Error callback
});
```

### useDeviceType Hook

```typescript
const {
  type,          // 'desktop' | 'mobile-web' | 'mobile-app'
  isMobile,      // boolean
  isTablet,      // boolean
  isDesktop,     // boolean
  isNativeApp,   // boolean
  userAgent,     // string
  screenWidth,   // number
  screenHeight,  // number
} = useDeviceType();
```

---

## 🎯 Next Steps

1. **Test on Production**: Deploy and test with real domain
2. **Monitor Logs**: Check for any edge cases
3. **Mobile App**: Build React Native app when ready
4. **Analytics**: Add tracking for verification success/failure rates
5. **A/B Testing**: Test different email templates

---

## 💡 Tips

- **Use Real Emails**: Test with real email addresses (Gmail, Outlook, etc.)
- **Check Spam**: Verification emails might land in spam initially
- **Mobile Testing**: Use BrowserStack or real devices
- **Cross-Browser**: Test on Safari, Chrome, Firefox, Edge
- **Slow Networks**: Test on 3G/4G to see loading states

---

## 📞 Support

If you encounter issues:
1. Check console logs (look for `[Verification]` or `[DeviceType]`)
2. Verify Supabase redirect URLs are correct
3. Test email delivery (check Supabase logs)
4. Check this guide's troubleshooting section

---

**Built with ❤️ for PipFactor**
