// __LOUNGE_VERSION__ is injected by esbuild (see scripts/build.js). The guard
// keeps this module importable under plain Node (the test runner), where the
// identifier is undefined.
export const APP_VERSION = typeof __LOUNGE_VERSION__ !== 'undefined' ? __LOUNGE_VERSION__ : '0.0.0';
