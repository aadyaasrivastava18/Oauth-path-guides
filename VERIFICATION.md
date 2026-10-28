# Verification notes

This file records what was checked, what was corrected, and what could not be verified. Fill in the tables as you verify; leave a row as "Could not verify" rather than guessing.

## 1. What automated tests cover (offline, 40 tests)

Gemini, Tavily and Jina are replaced by fakes. These parts run for real: quote verification, rule-based grading, assembly, the endpoint prober (against a mock HTTP server), SSRF guard, store, rate limiter, web routes, streaming, and HTML escaping.

Bugs the tests caught during the build (all fixed, each now has a regression test):

| Bug | Why it mattered |
|---|---|
| Malformed model output (a string where a list was expected) made the guide look like it had real registration steps | A guide could claim "OAuth supported, grade 1" from garbage |
| Evidence bar used an inline `style=` attribute, which the Content-Security-Policy blocks | It would have rendered blank in a browser; tests now fail on any inline style, script or handler |

Design corrections from reading the sources:

| Assumption | What the source said | Change |
|---|---|---|
| The managed-auth directory could be snapshotted as a static list | The page loads its list dynamically and points to `composio_managed_auth_schemes` in the API/SDK | Live lookup via the SDK instead of a snapshot |
| Gemini's free tier includes built-in search grounding | Third-party reports say it does not on current models | Search is a separate step (Tavily) |

## 2. Guide verification (do this by hand, about 15 minutes per guide)

For each guide you plan to show, open the provider's real developer console or docs and check these five claims. Then set `"hand_verified": true` in that guide's JSON only if you checked it.

| App | Claim checked | Guide said | Reality (link, date) | Verdict (Confirmed / Corrected / Could not verify) |
|---|---|---|---|---|
| GitHub | Console URL for creating an OAuth app | | | |
| GitHub | Review needed for production? | | | |
| Slack | Distribution / review requirement | | | |
| Gmail | Restricted-scope verification and security assessment | | | |
| LinkedIn | Partner program / approval requirement | | | |

Also check, for any guide: authorization and token URLs, redirect URI rules, and the grade shown.

## 3. Live-generation spot checks

Try apps you did not prepare. For each, record what worked and what did not.

| Input | Result (status, grade, evidence mix) | Problems found |
|---|---|---|
| (an app with OAuth, not in the library) | | |
| (an app with API keys only) | | |
| (a nonsense name) | | |

## 4. Known unverifiable

- Approval timelines: providers rarely publish them; shown as heuristics.
- Anything behind a login: we do not sign in to consoles.
