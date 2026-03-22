# Cloudflare WAF Configuration for Payments

To ensure that payment webhooks from Razorpay and NOWPayments are correctly processed by the backend, you need to configure Cloudflare's Web Application Firewall (WAF) to allow incoming requests from their IP addresses. If your backend is behind Cloudflare, default security levels or bot fight mode might block these legitimate server-to-server webhook requests.

## Razorpay Webhook Whitelist

Razorpay sends webhooks from a specific set of IP addresses. Create a WAF Custom Rule with the following configuration:

1. **Rule Name:** Allow Razorpay Webhooks
2. **Action:** Skip (or Allow)
3. **Expression (Expression Builder):** 
   - Non-interactive (Expression Editor):
     `(ip.src in {52.66.111.41 52.66.82.164 52.66.113.111 13.235.6.21})`
4. **WAF features to skip:** All remaining custom rules, Rate limiting rules, and Bot Fight Mode.

## NOWPayments IPN Whitelist

NOWPayments (Crypto) sends Instant Payment Notifications (IPN) from the following IP addresses:

1. **Rule Name:** Allow NOWPayments IPNs
2. **Action:** Skip (or Allow)
3. **Expression (Expression Editor):** 
   - `(ip.src in {130.162.59.88 130.162.59.39})`
4. **WAF features to skip:** All remaining custom rules, Rate limiting rules, and Bot Fight Mode.

## Endpoint Specific Rules (Optional but Recommended)

For enhanced security, you should restrict these allowed IPs to only your webhook paths:
* `/api/webhooks/razorpay`
* `/api/webhooks/nowpayments`

Example combined expression for Cloudflare WAF:
```
(http.request.uri.path eq "/api/webhooks/razorpay" and ip.src in {52.66.111.41 52.66.82.164 52.66.113.111 13.235.6.21}) or 
(http.request.uri.path eq "/api/webhooks/nowpayments" and ip.src in {130.162.59.88 130.162.59.39})
```
