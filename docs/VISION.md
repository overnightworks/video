# video vision

## What it does

A service that gives any app a live, photoreal video face for its AI agents: the
app hands it an agent's spoken sentences as audio and a few facts (speaking,
cut; later mood, listening, thinking, a gesture), and each viewer receives one
live stream of picture and sound. The model behind it is chosen by
setting, on the operator's card or at a hosted service he names, so a model, a card, a hosted
service or a kind of look changes without changing the app. The figure
stays the fallback: an app shows it whenever this service cannot serve.

## Who uses it today

Nobody yet. hopin's agent tiles are the first caller, through its server and
page; personas names each agent's looks.

## Deliberate non-goals

- Not a voice: it speaks nothing; the sound it streams is the app's sentence
  audio, unchanged.
- Not a mind: it decides no words and no motion beyond what a fact asks.
- Not the fallback: figure is; an app shows the figure whenever this service
  cannot serve.
- Not a media server for calls: it streams only the agents it renders, never a
  person's media.
- Not a store: it keeps nothing it hears or shows; it downloads no weights, and
  fetches each configured look once at start, by digest, never during a session.
- Not sharing by default: the sentence audio and a look's picture reach an outside service only
  when the operator's setting names a hosted service, which then receives both to render the face.
- Not trusting: every byte from an app or a look source is bounded input.
