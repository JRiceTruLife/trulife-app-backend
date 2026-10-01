# Affiliate tracking release

Prepared October 1, 2026. This release strengthens the existing affiliate portal without enabling the paywall or submitting either store app.

## Behavior

- Each affiliate gets the link/code belonging to its authenticated affiliate account. Ordinary affiliates see only their own customers, sales and totals; authorized administrators retain program-wide oversight.
- First valid referral is remembered for up to 90 days before signup when browser storage is available. Signup saves the referral server-side for subsequent purchases. An optional invite-code field covers cross-device or fresh app installs; automatic deferred deep linking through an app-store installation is not implemented.
- Existing attribution cannot be overwritten by subsequent links or the admin attach endpoint. Same-email self-referrals are excluded. This does not constitute comprehensive anti-fraud detection across different email accounts.
- Verified Stripe purchases cover every item in the current checkout catalog, including design guides and the 5 Step Method. Free access, administrative unlocks, simulated architecture purchases and external retailer purchases are not payment events.
- The new provider-neutral purchase ledger accepts other products through internal verified-payment adapters. It is not a claim that Apple/Google billing, external course checkout, subscriptions, or coaching checkout integrations already exist.
- Referral credit, receipt ledger and purchase entitlement commit together. Unique provider/transaction keys prevent duplicate credit. New commission rates are snapshotted at sale time.
- Cumulative verified refunds reduce commission; duplicate, partial, full and out-of-order refund notifications are handled for new ledger purchases. Historic purchases remain visible through the legacy guide history; legacy sales are not retroactively reconciled or re-rated.
- Manual commission entries and deletion of manual records are administrator-only. Verified payment rows cannot be manually deleted.
- Affiliate logout clears rendered customer/sales data and prevents stale asynchronous responses from displaying another session's information.
- The signup form and privacy policy disclose referral-related sharing. Affiliates receive first names and masked emails, not full contact details. Counsel review is still required; no legal approval is asserted.

## Verification

Run `python -m unittest -v test_affiliate_tracking` from the backend repository. The suite uses a fresh temporary database and mocked email delivery, never production payments.

15 tests cover isolation, auth namespaces, unsigned webhook rejection, signed purchase/refund webhooks, concurrent duplicate delivery, transaction rollback/retry, repeated purchases, generic product integration, refund ordering, rate snapshots, invalid/inactive referrals, self-referrals, immutable attribution, manual-sale restrictions and repeatable migrations.

Browser QA covers real signup from an invite URL, affiliate login, own tracking links, copy control, referred clients, purchase history, logout/account switching, empty states, 1365px desktop and 375px mobile viewports, and storage-blocked signup. No real purchase was charged.

## Production activation

Deploy backend and frontend together. Preserve the existing persistent database and environment settings. Required Stripe webhook subscriptions: `checkout.session.completed`, `checkout.session.expired`, `checkout.session.async_payment_succeeded`, and `charge.refunded`.

Before release, preserve a database backup using the hosting provider's backup/snapshot process. Migrations are additive. Do not drop the purchase ledger when rolling back application code.

## App-store handoff

Justin is supplying verification documents to Apple and Google. Document submission is not confirmed developer-account approval or app approval.

- Confirm both organization developer accounts are approved and agreements are complete.
- Create/confirm the Apple app record and numeric app ID, signing integration and bundle identifier `com.trulifeproperties.app`. The existing Codemagic configuration still has a placeholder Apple app ID.
- Review payment behavior for each store and target country before activating digital-content charges. Apple/Google purchase verification adapters are not part of this affiliate release.
- Implement and verify the account-deletion flows required for store submission. They were not present in the inspected API routes.
- Audit current SDK/target API requirements, privacy manifests, store privacy/data-safety declarations, reviewer credentials, content rights, and legal review.
- Rebuild Android and iOS from the final release configuration; test on real devices through internal testing/TestFlight before production submission.
- Keep `PAYWALL_ENABLED = false` until the user explicitly authorizes changing it.
- Existing mobile wrappers load `https://app.trulifeproperties.com`; deployed web changes appear there without changing the bundle ID. This does not replace native release testing or signed build uploads.
