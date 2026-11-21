# Watch the face live

Reader: the operator on the card host who wants to see and hear the face. One session,
one local page.

Needs: the browser package built (`pnpm run build` in `packages/browser`), the provider
prepared as in [the card proof](card-proof.md#prepare-flashhead-lite), `VIDEO_PROVIDER_ROOT`
set, and a mono 16-bit WAV per sentence to speak.

```sh
VIDEO_PROVIDER_ROOT=$PWD/providers uv run --locked python scripts/watch_live.py \
  --log service.log --audio speech.wav --look-image face.png
```

`--dry-run` drives the `fake` provider with no card guard. Several `--audio` files are
chosen by file name on the page.

The command prints `watch at http://127.0.0.1:<port>/` once the service is ready. Open it,
press "Ansehen" (a browser plays sound only after a click), then use the buttons to speak a
file, clear the sentence or end.

It stops the service at the end button, at SIGINT or SIGTERM, at the 1800 s cap and when no
viewer has attached for a minute, and it writes nothing but `service.log`. It exits non-zero
when the session or the service ends by itself.
