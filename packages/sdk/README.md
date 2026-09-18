# statless-telemetry SDK

Zero-dependency, privacy-first usage telemetry for developer CLIs and npm packages.
Under 2KB gzipped, no runtime dependencies, non-blocking, and silent on failure.

```bash
npm install statless-telemetry
```

```ts
import { track } from "statless-telemetry";

const started = Date.now();
// ... run the command ...
void track({
  package: "mytool",
  version: "1.2.3", // read from your package.json
  command: "build",
  durationMs: Date.now() - started,
});
```

Point the SDK at your own collector with `STATLESS_TELEMETRY_URL` or
`configure({ endpoint })`. Private collectors that set `INGEST_TOKEN` are
supported with `STATLESS_TELEMETRY_TOKEN`.

## Privacy

No request is ever made when `DO_NOT_TRACK=1` or `STATLESS_OPTOUT=1` is set.
The payload contains only the package name, version, command, duration, Node
major version, OS platform, and a CI yes/no flag - never paths, arguments, or
command output. Requests are capped at 500ms with `AbortSignal.timeout` and
fail silently, so a collector outage cannot crash or lag your CLI.

See the [repository README](https://github.com/dimitrisx26/statless-telemetry#readme)
for Commander.js and Yargs integrations.

## License

MIT
