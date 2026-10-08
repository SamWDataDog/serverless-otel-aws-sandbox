// Root cause (Round 4 follow-up): AWS Lambda's nodejs24.x runtime loads the
// user handler module via `await import(...)`, not `require(...)` --
// confirmed via web search against a documented OTel JS Lambda
// auto-instrumentation requirement, and directly observed here (the
// AwsLambdaInstrumentation require-hook's own "patch handler function"
// debug line never fired, even though the hook was correctly registered).
// require-in-the-middle (what `--require instrument` sets up) only
// intercepts CommonJS require() calls. Registering this ESM loader hook
// via `--import` is what actually lets the instrumentation see the
// handler module being loaded. See FINDINGS.md "Round 4".
import { register } from 'node:module';
register('@opentelemetry/instrumentation/hook.mjs', import.meta.url);
