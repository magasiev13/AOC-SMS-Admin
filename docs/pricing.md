# Twinevia Pricing

Twinevia launch pricing uses paid activation plus a paid monthly subscription. Production should not fund a live Twilio trial before the customer has paid.

## Public Plans

| Plan | Monthly price | Included outbound SMS segments |
|---|---:|---:|
| Starter | $49/mo | 1,000 |
| Growth | $99/mo | 3,000 |
| Scale | $199/mo | 10,000 |

Additional outbound SMS segments are billed at `$0.03` per segment through the monthly overage invoice-item reconciliation.

Activation is a one-time `$149` Stripe price charged during the first paid checkout. Twinevia provider provisioning, number purchase, and A2P submission remain gated behind active billing.

## Stripe Setup

Create one Stripe product named `Twinevia Messaging Workspace`.

Create these Stripe prices:

- one-time activation price: `$149`
- recurring Starter price: `$49/mo`
- recurring Growth price: `$99/mo`
- recurring Scale price: `$199/mo`

Configure production with:

```env
STRIPE_ACTIVATION_PRICE_ID=price_...
STRIPE_PRICE_ID=price_...          # Starter recurring price
STRIPE_GROWTH_PRICE_ID=price_...
STRIPE_SCALE_PRICE_ID=price_...
BILLING_TRIAL_DAYS=0
BILLING_STARTER_INCLUDED_OUTBOUND_SEGMENTS=1000
BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS=3000
BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS=10000
BILLING_OUTBOUND_SEGMENT_RATE_USD=0.0300
```

`STRIPE_PRICE_ID` remains the default Starter recurring price for backward compatibility. `BILLING_INCLUDED_OUTBOUND_SEGMENTS` remains only as the fallback allowance when a subscription has an unknown Stripe price ID.

## Operational Rules

- First paid checkout includes the activation price and the selected recurring plan.
- A canceled organization that already has Stripe customer or subscription history is not charged activation again when it resubscribes.
- Stripe Portal upgrades and downgrades must use the configured recurring price IDs so webhook sync can map the subscription to Starter, Growth, or Scale allowances.
- Complimentary internal workspaces can remain on the existing platform-admin complimentary billing path.
- Usage is measured in SMS segments, not contacts or recipients.

## Cost Floor And Benchmark

Current launch assumptions use Twilio low-volume A2P fees, US long-code SMS pricing, local phone-number rent, Stripe card processing, and Stripe Billing fees as the cost floor. AOC is an internal usage benchmark, not the public baseline: a small community can still land in Growth or Scale when messages are long, so allowances are segment-based.

Recheck vendor pricing immediately before launch. The original research references were:

- [Twilio SMS pricing](https://www.twilio.com/en-us/sms/pricing/usa)
- [Twilio A2P fees FAQ](https://help.twilio.com/articles/11587910480155)
- [Twilio A2P overview](https://www.twilio.com/en-us/phone-numbers/a2p-10dlc)
- [Stripe Billing pricing](https://stripe.com/billing/pricing)
- [Stripe US pricing](https://stripe.com/us/pricing)
- [EZ Texting pricing](https://www.eztexting.com/pricing)
- [SlickText pricing](https://www.slicktext.com/pricing)
- [Textedly pricing help](https://help.textedly.com/en/articles/473052-how-much-does-textedly-cost)
