# Twinevia Pricing

Twinevia launch pricing uses a paid setup fee plus a recurring subscription. Production should not fund a live Twilio trial before the customer has paid.

## Public Plans

| Option | Subscription price | Included outbound SMS segments |
|---|---:|---:|
| Monthly | $59.99/mo | 1,000/month |
| Annual upfront | $600/year | 1,000/month |

Additional outbound SMS segments are billed at `$0.03` per segment through the monthly overage invoice-item reconciliation.

Setup is a one-time `$149.99` Stripe price charged during the first paid checkout for either subscription option. Twinevia provider provisioning, number purchase, and A2P submission remain gated behind active billing.

## Stripe Setup

Create one Stripe product named `Twinevia Messaging Workspace`.

Live Stripe product created/used for launch:

- Product: `prod_UOdKHjO2FOXhxL` (`Twinevia Messaging Workspace`)
- Monthly recurring: `price_1TYtNuEksbf3Q3FgN2B1VqGN` (`$59.99/mo`)
- Annual recurring: `price_1TYtO4Eksbf3Q3FgHzXB9S5b` (`$600/year`)
- One-time setup fee: `price_1U9Bl6Eksbf3Q3FgcJ0YRJ05` (`$149.99`)

Create these Stripe prices:

- one-time setup price: `$149.99`
- recurring monthly price: `$59.99/mo`
- recurring annual price: `$600/year`

Configure production with:

```env
STRIPE_ACTIVATION_PRICE_ID=price_1U9Bl6Eksbf3Q3FgcJ0YRJ05
STRIPE_MONTHLY_PRICE_ID=price_1TYtNuEksbf3Q3FgN2B1VqGN
STRIPE_ANNUAL_PRICE_ID=price_1TYtO4Eksbf3Q3FgHzXB9S5b
STRIPE_PRICE_ID=price_1TYtNuEksbf3Q3FgN2B1VqGN
BILLING_TRIAL_DAYS=0
BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS=1000
BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS=1000
BILLING_OUTBOUND_SEGMENT_RATE_USD=0.0300
BILLING_ACTIVATION_FEE_USD=149.99
```

`STRIPE_PRICE_ID` remains the default monthly recurring price for backward compatibility. `BILLING_INCLUDED_OUTBOUND_SEGMENTS` remains only as the fallback allowance when a subscription has an unknown Stripe price ID.

## Operational Rules

- First paid checkout includes the activation price and the selected recurring plan.
- Staged annual checkout uses the same activation price, collecting it before provider review and collecting the annual subscription only after provider approval.
- A canceled organization that already has Stripe customer or subscription history is not charged activation again when it resubscribes.
- Stripe Portal updates must use the configured recurring price IDs so webhook sync can map the subscription to monthly or annual allowances.
- Complimentary internal workspaces can remain on the existing platform-admin complimentary billing path.
- Usage is measured in SMS segments, not contacts or recipients.
- For a first client who should only see the `$600/year` upfront offer, open the platform admin organization Access page and enable annual-only checkout in Billing State before sending them to checkout.
- `BILLING_ANNUAL_ONLY_ORG_SLUGS` and `BILLING_ANNUAL_ONLY_ORG_IDS` remain as break-glass config overrides, not the normal operating path.

## Cost Floor And Benchmark

Current launch assumptions use Twilio low-volume A2P fees, US long-code SMS pricing, local phone-number rent, Stripe card processing, and Stripe Billing fees as the cost floor. AOC is an internal usage benchmark, not the public baseline; allowances are segment-based because a small customer can still create overage with long messages.

Recheck vendor pricing immediately before launch. The original research references were:

- [Twilio SMS pricing](https://www.twilio.com/en-us/sms/pricing/usa)
- [Twilio A2P fees FAQ](https://help.twilio.com/articles/11587910480155)
- [Twilio A2P overview](https://www.twilio.com/en-us/phone-numbers/a2p-10dlc)
- [Stripe Billing pricing](https://stripe.com/billing/pricing)
- [Stripe US pricing](https://stripe.com/us/pricing)
- [EZ Texting pricing](https://www.eztexting.com/pricing)
- [SlickText pricing](https://www.slicktext.com/pricing)
- [Textedly pricing help](https://help.textedly.com/en/articles/473052-how-much-does-textedly-cost)
