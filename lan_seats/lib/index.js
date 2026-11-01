// Host-side half of lan-seats: the plugin is loaded for its client bundle, so the host plugin only needs to
// exist and expose a name. Sidebar rendering and the bridge calls live in ./client.js.

export const name = 'lan-seats-host'

export function apply() {}
