# Jev Ultrafast

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The task remains one natural-language goal. Do not add site-specific plans or hardcoded field values to the policy. Optional caller-provided runtime text is exact URL/label/role-scoped data for a Jev-selected TYPE_TEXT target, never selectors or an operation plan; it does not expand task authorization.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT uses matching pending_text when available; otherwise the host/text LLM supplies the value according to the workflow. Explicit caller-scoped text can satisfy that host handoff. A generated stale retry value may be cached only while its entire helper input is identical; prepared entries are consumed before the attempt and never restored after a stale rejection. Credentials, payment data, OTPs, and other secrets must not be prepared.
- Never retry a browser mutation. Log execution before observing its result.
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, uv build.
