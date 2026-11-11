# NFT Sales Bot

Posts every sale of your NFT collection to X:

```
🎀 moemoe LLC #4266 Bought
💰 0.009753 ETH ($20.00)
🛒 OpenSea
👤 0x33aa...f5ed → 0x44d1...214a
```

Works on Ethereum, Base, ApeChain, Berachain, MegaETH and 15 more chains.

## You need

- [Docker](https://docs.docker.com/get-docker/) (free)
- An X account for the bot
- Your collection's contract address (on your OpenSea page)

## 1. Add your collection

In this folder, create a file named `.env` (in Terminal: `touch .env && open -e .env`) and paste this in:

```
CHAIN=ethereum
CONTRACT_ADDRESS=0x06f97e756f5c7ac0db75411101bd09e17f63aba7
COLLECTION_NAME=moemoe LLC
EMOJI=🎀

X_API_KEY=
X_API_SECRET=
X_ACCESS_TOKEN=
X_ACCESS_TOKEN_SECRET=

DRY_RUN=true
```

Change the first 4 lines to your collection (they're filled in for moemoe LLC).
Keep `.env` private: it will hold your X keys.

`CHAIN` is one of: `ethereum`, `base`, `arbitrum`, `optimism`, `polygon`, `abstract`, `apechain`,
`berachain`, `megaeth`, `monad`, `hyperevm`, `ink`, `mantle`, `zora`, `shape`, `unichain`,
`soneium`, `avalanche`, `sei`, `robinhood`. On another EVM chain, use its chain ID and add
`RPC_URL=` (plus `NATIVE_SYMBOL=` if its coin isn't ETH).

## 2. Add your X keys

1. Log in to **developer.x.com** as the bot account and buy some API credits.
2. Create an app and set it to **Read and write**.
3. Copy the 4 keys from **Keys and tokens** into `.env`.

> Make the Access Token **after** choosing "Read and write", or posting won't work.

## 3. Test it

```bash
docker compose build
docker compose run --rm bot python bot.py --check
docker compose run --rm bot python bot.py --recent
```

`--recent` shows what the posts for your last 3 sales would look like. Nothing is posted yet.

## 4. Turn it on

In `.env`, change `DRY_RUN=true` to `DRY_RUN=false`, then:

```bash
docker compose up -d
```

It keeps running and posts each new sale. Stop it with `docker compose down`.

## Tips

- **Cost:** about $0.015 per post on X.
- **Change the post text:** edit `tweet_template.txt`.
- **Skip small sales:** add `MIN_PRICE_USD=10` to `.env`.
- **Something wrong?** Run `docker compose logs -f` to see what the bot is doing.
