# Rosyy Bot ❀ (v2)

Group manager + chatty Rosyy (Odisha) + coin games. Ek file: `bot.py`. Render **Web Service**.

## Render env vars (sirf yahi, kabhi file/screenshot me mat daalna)
- `BOT_TOKEN` — BotFather token
- `MONGO_URI` — main MongoDB (settings bhi isi me save hoti hain)
- `GEMINI_API_KEY`, `GEMINI_API_KEY_2` — Google AI Studio keys (aur keys Telegram se `/addkey` kar sakte ho)
- `OWNER_ID` — default `8536757095`

Baaki sab **Telegram se** (owner): `/panel`, `/set`, `/config`.

## Gemini
Models ki chain: pehla model 15s (`/set ai_timeout`) me jawab na de, ya fail ho, to next model. Key rate-limit (429) ho to dusri key.
`/models`, `/models live`, `/models sync`, `/addmodel`, `/delmodel`, `/topmodel`, `/addkey`, `/delkey`, `/keys`, `/testai`.
Pics, voice notes sab Gemini se hi samajhta hai.

## Owner commands
`/setjoin` (group me bhejo; bot us group me admin ho), `/unsetjoin`, `/joinlist` — DM me join gate.
`/addpack @name`, `/delpack`, `/packs` — stickers. `/addmongo`, `/delmongo`, `/mongos`, `/usemongo` — multi MongoDB (full/down ho to next par failover).
`/addcoins`, `/takecoins`, `/set <key> <value>`.

## Games
/daily /bal /pay /top /rob /kill /revive /protect 1d|2d /fd /invest /toss /slots + slap, punch, hug... (coins lagte hain). Balance MongoDB me permanent save hota hai.

Local: `export BOT_TOKEN="..." GEMINI_API_KEY="..."; python bot.py`

## Chat behaviour
Voice note replies default OFF (`/set voice_reply 1` to enable). Replies are short (1-2 lines). Group me kabhi kabhi random reaction/sticker (`react_chance`, `sticker_chance`). Welcome + roast goodbye: `/welcome on|off`, `/goodbye on|off`.
