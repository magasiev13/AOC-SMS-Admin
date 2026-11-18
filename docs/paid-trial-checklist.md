# First paid workspace trial

Use an internal organization with its own owner, approved sender, and one consenting recipient controlled by the trial operator. Record the release, organization ID, dates, approved spending limit, expected subscription and setup charges, and the operator in a private trial receipt. The cardholder completes checkout directly in Stripe. Keep card details, secrets, invitation links, phone numbers, and message content out of the receipt.

| Step | Action | Evidence to retain |
|---|---|---|
| Application | Submit the public pilot request for the internal workspace. In the platform console, approve that application and issue its owner invitation. | Application ID, approved state, organization ID. |
| Access | Open the invitation in a fresh session. Establish the owner account and review and accept the current policies. | Membership role and policy versions with acceptance times. |
| Checkout | Review the offered plan, setup charge, tax, billing interval and renewal amount against the spending limit. Complete the hosted checkout. | Checkout and subscription IDs, paid invoice amount, verified webhook state in Billing. A browser return alone is insufficient. |
| Registration | Complete actual provider registration using accurate business information and wait for provider approval. Use a provider-approved sender connected to this organization. | Sender readiness and provider approval time. |
| Reviewed send | Import the operator’s consenting contact with its actual consent evidence. Review one short message, its single recipient, and its segment estimate. Confirm once. | Send ID, submission outcome, final carrier receipt if available, confirmed segments. |
| Settlement | Inspect the usage row and billing period after provider facts finalize. Wait for the normal settlement boundary. Reconcile the posted amount, allowance, and any late carry-forward with the Stripe invoice or zero-overage state. | Period dates, confirmed units, invoice/settlement identifiers and amounts. |
| Cancellation | Use the billing portal for this specific test subscription. Confirm the cancellation effective date in both Stripe and Twinevia. Recheck renewal and any outstanding balance after settlement. | Cancellation webhook/event and final subscription state, outstanding amount or zero, confirmation of renewal status. |

Use the existing [billing transition guide](billing-transitions.md) to investigate a pending or conflicting payment before retrying. The trial receipt is complete when every row above has actual evidence or an explicitly recorded outstanding dependency.
