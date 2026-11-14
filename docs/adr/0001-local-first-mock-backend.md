# Local-first mock backend for v1

V1 runs entirely in the browser. Accounts, Credits, Chats, Generations, Likes, and Assets are persisted in localStorage and IndexedDB behind repository interfaces, and the provider layer returns fixtures instead of calling real APIs. This keeps the app deployable as a static frontend and demoable without secrets, at the cost of no cross-device sync and demo-only password hashing; a real backend replaces the repository and provider implementations without changing the UI.

## Consequences

- Never use a real password in the demo. Passwords are salted and hashed with Web Crypto, but localStorage is not a trust boundary.
- Every read and write goes through a repository interface so the backend swap is mechanical.
