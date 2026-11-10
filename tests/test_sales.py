"""Sale detection against real Robinhood Chain and Ethereum transactions saved in tests/fixtures.

Run with:  .venv/bin/python -m unittest discover tests
"""
import copy
import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chain import ORDER_FULFILLED_TOPIC, RPC, RPCError, nft_transfers, sales_in_transaction  # noqa: E402
from config import CHAINS, NATIVE, load_settings  # noqa: E402
from prices import Pricer  # noqa: E402
from media import _gateway_urls  # noqa: E402
from ens import namehash  # noqa: E402
from tweet import check_template, format_amount, format_tweet, template_problems  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
WHALESTREET = "0x01f33271d74a9dcc91798c7232d3d788da93a1f6"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

ETHEREUM = CHAINS["ethereum"]
ETH_WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
MILADY = "0x5af0d9827e0c53e4799bb226655a1de152a425a5"
AZUKI = "0xed5af388653567af2f388e6224dc7c4b3241c544"
PUDGY = "0xbd3531da5cf5857e7cfaa92426877b022e612cf8"
CRYPTOSKULLS = "0xc1caf0c19a8ac28c41fe59ba6c754e4b9bd54de9"
MOEMOE = "0x06f97e756f5c7ac0db75411101bd09e17f63aba7"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def sales(fixture, contract, chain=CHAINS["robinhood"]):
    ours = [entry for entry in fixture["receipt"]["logs"] if entry["address"].lower() == contract]
    return sales_in_transaction(contract, nft_transfers(ours), fixture["receipt"], fixture["tx"], chain)


class SaleDetectionTest(unittest.TestCase):
    def test_listing_bought_with_eth(self):
        [sale] = sales(load("listing_eth"), WHALESTREET)
        self.assertEqual(sale.token_id, 2863)
        self.assertEqual(sale.payment_token, NATIVE)
        self.assertEqual(sale.amount, 3849000000000000)  # equals the ETH the buyer sent
        self.assertEqual(sale.marketplace, "OpenSea")

    def test_two_sales_in_one_transaction(self):
        found = sales(load("two_sales_one_tx"), WHALESTREET)
        self.assertEqual(sorted(s.token_id for s in found), [2336, 2918])
        self.assertTrue(all(s.amount == 3100000000000000 for s in found))
        self.assertEqual(len({s.id for s in found}), 2)

    def test_accepted_offer_reports_gross_price_not_sellers_cut(self):
        # The bidder paid 0.0031 WETH; the seller's mirror order only shows their 0.002914 after fees.
        [sale] = sales(load("matched_bid_weth"), "0x481ba120a6632714d8c872d1f4b6b57c8769dc21")
        self.assertEqual(sale.token_id, 2577)
        self.assertEqual(sale.payment_token, WETH)
        self.assertEqual(sale.amount, 3100000000000000)
        self.assertEqual(sale.seller, "0x3483216ecaacd0c36170a07530983e895bf21825")
        self.assertEqual(sale.buyer, "0x443575ac1114d1166e57dd16cc10cee38575f9f9")

    def test_router_hop_is_one_sale_to_the_final_owner(self):
        # Bought into a multicall contract, then forwarded to the user in the same tx.
        [sale] = sales(load("router_hop_usdg"), "0x1a6eb6a352a7278fec853a7c4900ff7917fc8a94")
        self.assertEqual(sale.token_id, 628)
        self.assertEqual(sale.payment_token, USDG)
        self.assertEqual(sale.amount, 1800000)  # 1.80 USDG
        self.assertEqual(sale.seller, "0xb2d2fa4a9cd4afb9652a78f58bed22a95760d114")
        self.assertEqual(sale.buyer, "0xd26b28938c5572fe888fd201a96fec53daa71b4b")

    def test_transfer_without_payment_is_not_a_sale(self):
        fixture = copy.deepcopy(load("listing_eth"))
        fixture["receipt"]["logs"] = [
            entry for entry in fixture["receipt"]["logs"] if entry["topics"][0] != ORDER_FULFILLED_TOPIC
        ]
        fixture["tx"]["value"] = "0x0"
        self.assertEqual(sales(fixture, WHALESTREET), [])


class FormattingTest(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(format_amount(Decimal("1.06")), "1.06")
        self.assertEqual(format_amount(Decimal("0.003849")), "0.003849")
        self.assertEqual(format_amount(Decimal("0.0031")), "0.0031")
        self.assertEqual(format_amount(Decimal("0.123456")), "0.1235")
        self.assertEqual(format_amount(Decimal("1234.5")), "1,234.5")
        self.assertEqual(format_amount(Decimal("2")), "2")

    def test_tweet_matches_reference_layout(self):
        text = format_tweet(
            emoji="🐳", name="Whale Street", token_id=2863, price=Decimal("0.003849"), symbol="ETH",
            usd=Decimal("9.8634"), marketplace="OpenSea",
            seller="0xea0e00000000000000000000000000000000d9b1", buyer="0xb21a000000000000000000000000000000002ed4",
        )
        self.assertEqual(text, "🐳 Whale Street #2863 Bought\n💰 0.003849 ETH ($9.86)\n🛒 OpenSea\n👤 0xea0e...d9b1 → 0xb21a...2ed4")

    def test_unknown_marketplace_line_is_dropped(self):
        text = format_tweet(
            emoji="🐳", name="Whale Street", token_id=1, price=Decimal("1"), symbol="ETH", usd=None,
            marketplace=None, seller="0x" + "1" * 40, buyer="0x" + "2" * 40,
        )
        self.assertNotIn("🛒", text)
        self.assertIn("💰 1 ETH\n", text)

    def test_ens_names_replace_addresses(self):
        text = format_tweet(
            emoji="🐳", name="Whale Street", token_id=1, price=Decimal("1"), symbol="ETH", usd=None,
            marketplace="OpenSea", seller="0x" + "1" * 40, buyer="0x" + "2" * 40, buyer_name="whale.eth",
        )
        self.assertTrue(text.endswith("👤 0x1111...1111 → whale.eth"))

    def test_namehash(self):
        self.assertEqual(namehash("addr.reverse").hex(), "91d1777781884d03a6757a803996e38de2a42967fb37eeaca72729271025a9e2")
        self.assertEqual(namehash("").hex(), "00" * 32)

    def test_ipfs_uris(self):
        self.assertEqual(_gateway_urls("ipfs://Qm123/7", "https://gw.example/ipfs/")[0], "https://gw.example/ipfs/Qm123/7")
        self.assertEqual(_gateway_urls("ipfs://ipfs/Qm123", "https://gw.example/ipfs/")[0], "https://gw.example/ipfs/Qm123")
        self.assertEqual(_gateway_urls("ar://abc", "x"), ["https://arweave.net/abc"])

    def test_template_typos_are_reported(self):
        self.assertEqual(template_problems(["{emoji} {nmae}"]), ["Line 1: unknown placeholder {nmae}"])
        self.assertEqual(check_template(), [])  # the tweet_template.txt that ships is valid


class EthereumSaleTest(unittest.TestCase):
    def test_opensea_listing_bought_with_eth(self):
        [sale] = sales(load("eth_opensea_listing"), MILADY, ETHEREUM)
        self.assertEqual((sale.token_id, sale.payment_token, sale.amount), (3089, NATIVE, 659999000000000000))
        self.assertEqual(sale.marketplace, "OpenSea")

    def test_opensea_offer_paid_in_weth(self):
        [sale] = sales(load("eth_opensea_offer_weth"), MILADY, ETHEREUM)
        self.assertEqual((sale.payment_token, sale.amount), (ETH_WETH, 623000000000000000))  # all the WETH the bidder sent
        self.assertEqual(sale.seller, "0x8c85f3e4d45e02f65cd67d22d0d33518fb180730")
        self.assertEqual(sale.buyer, "0x4459084da2d3a774c436f2e75f2e3fe9335dc5de")

    def test_blur_sweep_prices_each_nft(self):
        # One buyer, four Blur listings at different prices: each post gets its own price, not the average.
        fixture = load("eth_blur_sweep")
        found = sales(fixture, CRYPTOSKULLS, ETHEREUM)
        self.assertEqual({s.token_id: s.amount for s in found}, {
            2941: 38354118800000220, 2962: 39136855900000220, 7635: 37587036400000220, 9321: 36835295700000220,
        })
        self.assertEqual(sum(s.amount for s in found), int(fixture["tx"]["value"], 16))
        self.assertTrue(all(s.marketplace == "Blur" and s.payment_token == NATIVE for s in found))

    def test_blur_bids_accepted_in_one_transaction(self):
        found = sales(load("eth_blur_bids"), AZUKI, ETHEREUM)
        self.assertEqual({s.token_id: s.amount for s in found}, {
            3762: 410000000000000000, 7460: 400000000000000000, 6328: 400000000000000000,
            5188: 410000000000000000, 710: 400000000000000000,
        })
        self.assertEqual(len({s.buyer for s in found}), 5)  # five different bidders

    def test_sweep_on_scatter_labels_each_nft_by_its_listing(self):
        # One purchase on Scatter's site: two Scatter listings and one OpenSea listing that Scatter shows.
        found = {s.token_id: (s.amount, s.marketplace) for s in sales(load("eth_scatter_listing"), MOEMOE, ETHEREUM)}
        self.assertEqual(found, {1981: (20986455000000000, "Scatter"), 4989: (20500000000000000, "Scatter"),
                                 3414: (17870099999000000, "Scatter")})

    def test_opensea_listing_bought_on_scatter(self):
        # The price is the OpenSea listing price; Scatter adds its 5% fee on top for the buyer.
        [sale] = sales(load("eth_opensea_via_scatter"), MOEMOE, ETHEREUM)
        self.assertEqual((sale.token_id, sale.payment_token, sale.amount, sale.marketplace),
                         (1820, NATIVE, 6760900000000000, "Scatter"))

    def test_scatter_listing_bought_elsewhere(self):
        # Sent straight to Seaport, not through Scatter's site: the listing is still Scatter's.
        [sale] = sales(load("eth_scatter_listing_direct"), MOEMOE, ETHEREUM)
        self.assertEqual((sale.token_id, sale.amount, sale.marketplace), (381, 25000000000000000, "Scatter"))

    def test_scatter_offer_accepted(self):
        [sale] = sales(load("eth_scatter_offer"), MOEMOE, ETHEREUM)
        self.assertEqual((sale.token_id, sale.payment_token, sale.amount, sale.marketplace),
                         (937, ETH_WETH, 10450000000000000, "Scatter"))

    def test_mixed_transaction_labels_each_nft_by_its_own_order(self):
        # An OpenSea listing and a Scatter listing filled in one Seaport transaction (built from two real ones).
        opensea, scatter = load("eth_opensea_listing"), load("eth_scatter_listing_direct")
        mixed = {"receipt": {"logs": opensea["receipt"]["logs"] + scatter["receipt"]["logs"]}, "tx": opensea["tx"]}
        [milady], [moemoe] = sales(mixed, MILADY, ETHEREUM), sales(mixed, MOEMOE, ETHEREUM)
        self.assertEqual((milady.marketplace, moemoe.marketplace), ("OpenSea", "Scatter"))

    def test_nftx_pool_purchase(self):
        [sale] = sales(load("eth_nftx_buy"), MOEMOE, ETHEREUM)
        self.assertEqual((sale.token_id, sale.amount, sale.marketplace), (4166, 6132124283215011, "NFTX"))

    def test_loan_repayments_are_not_sales(self):
        # The NFT comes back out of a lending contract (Blend, Gondi) and the borrower pays the lender.
        self.assertEqual(sales(load("eth_blend_loan_repaid"), PUDGY, ETHEREUM), [])
        self.assertEqual(sales(load("eth_gondi_loan_repaid"), PUDGY, ETHEREUM), [])


class OtherChainsTest(unittest.TestCase):
    def test_ink_opensea_sale_paying_openseas_other_fee_wallet(self):
        [sale] = sales(load("ink_opensea"), "0x754c6d12651cbf927df960e8fe97cc8b38c8f558", CHAINS["ink"])
        self.assertEqual((sale.token_id, sale.payment_token, sale.amount, sale.marketplace), (814, NATIVE, 440000000000000, "OpenSea"))

    def test_megaeth_opensea_sale_on_openseas_alternate_seaport(self):
        [sale] = sales(load("megaeth_opensea"), "0x89ff7a37bf8851bcbee20b1032afc583f89b40ff", CHAINS["megaeth"])
        self.assertEqual((sale.token_id, sale.amount, sale.marketplace), (2100, 11798300000000000, "OpenSea"))

    def test_apechain_sale_is_priced_in_ape(self):
        [sale] = sales(load("apechain_opensea"), "0x81c9ce55e8214fd0f5181fd3d38f52fd8c33ec38", CHAINS["apechain"])
        self.assertEqual((sale.token_id, sale.amount, sale.marketplace), (2085, 2988 * 10 ** 18, "OpenSea"))
        pricer = Pricer(None, CHAINS["apechain"].payment_tokens)
        with mock.patch("prices.requests.get") as get:
            get.return_value.json.return_value = {"data": {"amount": "0.10"}}
            price, symbol, usd = pricer.describe(sale.payment_token, sale.amount)
        self.assertEqual((price, symbol, usd), (Decimal(2988), "APE", Decimal("298.80")))
        self.assertIn("APE-USD", get.call_args[0][0])

    def test_mantle_price_comes_from_coingecko(self):
        # Coinbase's "MNT" ticker is a different coin, so Mantle's MNT is priced by CoinGecko.
        pricer = Pricer(None, CHAINS["mantle"].payment_tokens)
        with mock.patch("prices.requests.get") as get:
            get.return_value.json.return_value = {"mantle": {"usd": 0.5}}
            self.assertEqual(pricer.describe(NATIVE, 10 ** 18), (Decimal(1), "MNT", Decimal("0.5")))
        self.assertIn("coingecko", get.call_args[0][0])


class SettingsTest(unittest.TestCase):
    def settings(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_settings()

    def test_robinhood_collection(self):
        s = self.settings(CHAIN="robinhood", CONTRACT_ADDRESS=WHALESTREET)
        self.assertEqual((s.chain.name, s.contract, s.collection_name), ("Robinhood Chain", WHALESTREET, ""))
        self.assertEqual((s.rpc_url, s.confirmations), ("https://rpc.mainnet.chain.robinhood.com", 20))

    def test_chain_is_required(self):
        with self.assertRaises(SystemExit):
            self.settings(CONTRACT_ADDRESS=WHALESTREET)

    def test_ethereum_collection(self):
        s = self.settings(CHAIN="Ethereum", CONTRACT_ADDRESS="0x5AF0D9827E0C53E4799BB226655A1DE152A425A5",
                          COLLECTION_NAME="Milady")
        self.assertEqual((s.chain.chain_id, s.contract, s.collection_name, s.confirmations), (1, MILADY, "Milady", 2))

    def test_ethereum_needs_a_contract_address(self):
        with self.assertRaises(SystemExit):
            self.settings(CHAIN="ethereum")

    def test_unknown_chain(self):
        with self.assertRaises(SystemExit):
            self.settings(CHAIN="solana", CONTRACT_ADDRESS=MILADY)

    def test_chain_by_id(self):
        s = self.settings(CHAIN="80094", CONTRACT_ADDRESS=MILADY)
        self.assertEqual((s.chain.name, s.chain.native, s.rpc_url), ("Berachain", "BERA", "https://rpc.berachain.com"))

    def test_any_other_chain_by_id_with_its_rpc(self):
        with self.assertRaises(SystemExit):  # an unknown chain needs its RPC
            self.settings(CHAIN="81457", CONTRACT_ADDRESS=MILADY)
        s = self.settings(CHAIN="81457", CONTRACT_ADDRESS=MILADY, RPC_URL="https://rpc.example", NATIVE_SYMBOL="eth")
        self.assertEqual((s.chain.chain_id, s.chain.native, s.rpc_url), (81457, "ETH", "https://rpc.example"))


def response(status, body):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode()
    return resp


class RPCTest(unittest.TestCase):
    def test_error_message_is_reported_even_with_an_http_error_status(self):
        rpc = RPC("https://rpc.example")
        rpc.session.post = mock.Mock(return_value=response(403, {"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32602, "message": "Archive requests require a personal token"}}))
        with self.assertRaisesRegex(RPCError, "Archive requests"):
            rpc.block_number()
        self.assertEqual(rpc.session.post.call_count, 1)  # not retried: it would fail the same way

    def test_get_logs_splits_ranges_the_rpc_says_are_too_big(self):
        rpc = RPC("https://rpc.example")

        def call(method, params):
            start, end = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            if end - start >= 100:
                raise RPCError("eth_getLogs: {'message': 'query exceeds max block range 100'}")
            return [(start, end)]

        rpc.call = call
        chunks = rpc.get_logs("0x" + "1" * 40, [], 0, 399)
        self.assertEqual((chunks[0][0], chunks[-1][1]), (0, 399))
        self.assertTrue(all(b[0] == a[1] + 1 for a, b in zip(chunks, chunks[1:])))  # no gaps
        self.assertTrue(all(end - start < 100 for start, end in chunks))

    def test_get_logs_does_not_split_other_errors(self):
        rpc = RPC("https://rpc.example")
        rpc.call = mock.Mock(side_effect=RPCError("eth_getLogs: {'message': 'Archive requests require a personal token'}"))
        with self.assertRaises(RPCError):
            rpc.get_logs("0x" + "1" * 40, [], 0, 4999)
        self.assertEqual(rpc.call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
