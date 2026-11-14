# Static-portable Next.js for the future desktop shell

The web app uses Next.js 16 but keeps the critical path client-side: no server actions, route handlers, or server-only data access. The future desktop shell is expected to reuse this frontend, and a static export is the cheapest portability guarantee. Marketing pages may still be statically rendered at build time; Workspace behavior must not depend on a Node server.

## Consequences

- Data access lives in client repositories, not in React Server Components.
- Any future server need — real auth, real generation, payments — arrives with the backend, not as an ad-hoc Next route handler.
