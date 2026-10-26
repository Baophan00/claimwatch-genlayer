"""Direct-mode tests for ClaimWatch.

    python -m pytest tests/direct -q

The genlayer-test harness mocks the web and the model, runs `run_check` as the
leader, and can replay the consensus callback as a validator, so the page each
side read can be inspected rather than assumed.
"""

import json
from datetime import datetime, timezone

CONTRACT = "contracts/claim_watch.py"

SUPPORTED_PAGE = "FETCHED-PAGE ClaimWatch docs: every ruling is stored on chain with the quote it relied on."
CHANGED_PAGE = "FETCHED-PAGE ClaimWatch docs: rulings are not stored, ask again next week."

WATCH_URL = r"https://example\.com/claimwatch"


def deploy(direct_deploy):
    return direct_deploy(CONTRACT)


def create(contract, direct_vm, sender, watch_id="watch-1", url="https://example.com/claimwatch"):
    direct_vm.sender = sender
    contract.create_watch(
        watch_id,
        url,
        "Rulings are stored on chain together with the quote they relied on.",
    )
    return watch_id


def mock_page(direct_vm, body):
    direct_vm.mock_web(WATCH_URL, {"status": 200, "body": body})


def mock_verdict(direct_vm, marker, verdict, quote="rulings are stored on chain", unmet=None, material=False):
    direct_vm.mock_llm(
        rf"(?s).*{marker}.*",
        json.dumps(
            {
                "verdict": verdict,
                "unmet": unmet or [],
                "material": material,
                "quote": quote,
                "reason": f"page said {marker}",
            }
        ),
    )


MULTI_CLAIM = """Rulings are stored on chain together with the quote they relied on.
Every check is a full consensus round.
Anyone may run a check on a watch."""


class TestCreate:
    def test_a_watch_records_the_claim_and_the_owner(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)

        watch = contract.get_watch("watch-1")
        assert watch["url"] == "https://example.com/claimwatch"
        assert "stored on chain" in watch["claim"]
        assert watch["owner"].lower() == str(direct_alice).lower() or watch["owner"].startswith("0x")
        assert watch["checks"] == "0"
        assert watch["last_verdict"] == ""
        assert contract.stats()["watches"] == 1

    def test_an_id_cannot_be_reused(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)
        with direct_vm.expect_revert("watch id already used"):
            contract.create_watch("watch-1", "https://example.com/other", "another claim")

    def test_the_url_must_be_http(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("url must be http or https"):
            contract.create_watch("watch-1", "ftp://example.com/page", "claim")

    def test_a_claim_is_required(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("claim must be 1..600"):
            contract.create_watch("watch-1", "https://example.com/page", "")

    def test_listing_returns_newest_first(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice, watch_id="watch-1")
        create(contract, direct_vm, direct_alice, watch_id="watch-2")

        rows = contract.list_watches(0, 10)
        assert [r["id"] for r in rows] == ["watch-2", "watch-1"]


class TestChecks:
    def test_a_check_records_the_verdict_and_the_quote(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)

        mock_page(direct_vm, SUPPORTED_PAGE)
        mock_verdict(direct_vm, "FETCHED-PAGE ClaimWatch docs", "supported")

        direct_vm.sender = direct_bob
        result = contract.run_check("watch-1")

        assert result["verdict"] == "supported"
        assert "stored on chain" in result["quote"]

        watch = contract.get_watch("watch-1")
        assert watch["checks"] == "1"
        assert watch["last_verdict"] == "supported"
        assert watch["last_checked_at"] != "0"

        history = contract.checks_of("watch-1")
        assert len(history) == 1
        assert history[0]["verdict"] == "supported"
        assert history[0]["seq"] == "1"
        assert contract.stats()["checks"] == 1

    def test_anyone_may_run_a_check(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)

        mock_page(direct_vm, SUPPORTED_PAGE)
        mock_verdict(direct_vm, "FETCHED-PAGE ClaimWatch docs", "supported")

        # Not the owner: the record is public, so checking is permissionless.
        direct_vm.sender = direct_bob
        contract.run_check("watch-1")
        assert contract.checks_of("watch-1")[0]["seq"] == "1"

    def test_history_keeps_every_ruling(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)

        mock_page(direct_vm, SUPPORTED_PAGE)
        mock_verdict(direct_vm, "FETCHED-PAGE ClaimWatch docs", "supported")
        direct_vm.sender = direct_bob
        contract.run_check("watch-1")

        # The page is edited between checks; the earlier ruling stays.
        direct_vm.clear_mocks()
        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored")
        contract.run_check("watch-1")

        history = contract.checks_of("watch-1")
        assert [h["verdict"] for h in history] == ["supported", "contradicted"]
        assert history[1]["seq"] == "2"
        assert contract.get_watch("watch-1")["last_verdict"] == "contradicted"
        assert contract.stats()["last_verdicts"]["contradicted"] == 1

    def test_an_unknown_watch_is_rejected(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("watch not found"):
            contract.run_check("nope")
        with direct_vm.expect_revert("watch not found"):
            contract.checks_of("nope")

    def test_an_unreadable_page_lands_as_unclear(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)

        def broken(request):
            raise RuntimeError("connection reset")

        direct_vm._live_web_handler = broken
        mock_verdict(direct_vm, "could not be read", "unclear", quote="")

        direct_vm.sender = direct_alice
        result = contract.run_check("watch-1")
        assert result["verdict"] == "unclear"
        assert result["quote"] == ""


class TestValidatorEvidence:
    """The page must be read inside the consensus callback, by each validator."""

    @staticmethod
    def _agree_on_verdicts(direct_vm):
        def hook(vm, request):
            template = request.get("ExecPromptTemplate")
            if template is None:
                return None
            leader = json.loads(template["leader_answer"])
            validator = json.loads(template["validator_answer"])
            return {"ok": leader["verdict"] == validator["verdict"]}

        direct_vm._gl_call_hook = hook

    def test_a_validator_reads_the_page_itself(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        create(contract, direct_vm, direct_alice)

        fetches = []
        page = {"body": SUPPORTED_PAGE}

        def serve(request):
            fetches.append(request["url"])
            return {"ok": {"response": {"status": 200, "body": page["body"].encode()}}}

        direct_vm._live_web_handler = serve
        mock_verdict(direct_vm, "FETCHED-PAGE ClaimWatch docs", "supported")
        self._agree_on_verdicts(direct_vm)

        direct_vm.sender = direct_bob
        assert contract.run_check("watch-1")["verdict"] == "supported"
        assert len(fetches) == 1

        # The page changes between the leader round and the validator round.
        page["body"] = CHANGED_PAGE
        direct_vm.clear_mocks()
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored")

        agreed = direct_vm.run_validator()

        assert len(fetches) == 2, "the validator did not read the page itself"
        assert fetches[0] == fetches[1] == "https://example.com/claimwatch"
        assert agreed is False, "validator agreed while reading a different page"


class TestBreachLayer:
    """A verdict becomes a consequence: material loss breaches, wording does not."""

    def watch(self, contract, direct_vm, direct_alice, watch_id="watch-1"):
        direct_vm.sender = direct_alice
        contract.create_watch(watch_id, "https://example.com/claimwatch", MULTI_CLAIM)
        return watch_id

    def test_the_claim_is_split_into_requirements(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)
        watch = contract.get_watch("watch-1")
        assert len(watch["requirements"]) == 3
        assert watch["requirements"][1].startswith("Every check")
        assert watch["state"] == "active"

    def test_more_than_six_requirements_are_refused(self, direct_vm, direct_deploy, direct_alice):
        contract = deploy(direct_deploy)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("at most 6 requirements"):
            contract.create_watch("watch-1", "https://example.com/claimwatch", "\n".join(f"promise {i}" for i in range(7)))

    def test_a_material_contradiction_breaches_the_watch(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored", unmet=[1, 2], material=True)

        direct_vm.sender = direct_bob
        result = contract.run_check("watch-1")

        assert result["state"] == "breached"
        assert result["unmet"] == [1, 2]
        assert result["material"] is True

        watch = contract.get_watch("watch-1")
        assert watch["state"] == "breached"
        assert watch["unmet"] == [1, 2]
        assert int(watch["cure_deadline"]) == int(watch["breach_at"]) + 7 * 24 * 3600
        assert watch["breaches"] == "1"

        assert contract.is_breached("https://example.com/claimwatch") is True
        assert contract.is_breached("https://example.com/other") is False
        assert [b["id"] for b in contract.breaches(0, 10)] == ["watch-1"]
        assert contract.stats()["states"]["breached"] == 1

    def test_a_cosmetic_contradiction_keeps_the_watch_active(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored", unmet=[1], material=False)

        direct_vm.sender = direct_bob
        assert contract.run_check("watch-1")["state"] == "active"
        assert contract.get_watch("watch-1")["state"] == "active"
        assert contract.is_breached("https://example.com/claimwatch") is False
        assert contract.breaches(0, 10) == []

    def test_a_second_contradiction_does_not_restart_the_breach(
        self, direct_vm, direct_deploy, direct_alice, direct_bob
    ):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored", unmet=[1], material=True)
        direct_vm.sender = direct_bob
        contract.run_check("watch-1")
        first = contract.get_watch("watch-1")

        contract.run_check("watch-1")
        again = contract.get_watch("watch-1")

        assert again["breaches"] == "1"
        assert again["breach_at"] == first["breach_at"]
        assert again["cure_deadline"] == first["cure_deadline"]

    def test_a_supported_check_inside_the_window_cures(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored", unmet=[1], material=True)
        direct_vm.sender = direct_bob
        contract.run_check("watch-1")
        assert contract.get_watch("watch-1")["state"] == "breached"

        direct_vm.clear_mocks()
        mock_page(direct_vm, SUPPORTED_PAGE)
        mock_verdict(direct_vm, "FETCHED-PAGE ClaimWatch docs", "supported")
        contract.run_check("watch-1")

        watch = contract.get_watch("watch-1")
        assert watch["state"] == "cured"
        assert watch["breaches"] == "1"
        assert watch["unmet"] == []
        assert contract.is_breached("https://example.com/claimwatch") is False
        assert contract.stats()["states"]["cured"] == 1

    def test_a_window_that_closes_turns_the_breach_into_a_failure(
        self, direct_vm, direct_deploy, direct_alice, direct_bob
    ):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored", unmet=[3], material=True)
        direct_vm.sender = direct_bob
        contract.run_check("watch-1")
        watch = contract.get_watch("watch-1")

        direct_vm.warp(datetime.fromtimestamp(int(watch["cure_deadline"]) + 60, timezone.utc).isoformat())

        assert contract.get_watch("watch-1")["state"] == "failed"
        assert contract.is_breached("https://example.com/claimwatch") is True
        assert contract.stats()["states"]["failed"] == 1
        with direct_vm.expect_revert("this watch is closed"):
            contract.run_check("watch-1")

    def test_a_supported_verdict_cannot_name_a_missing_requirement(
        self, direct_vm, direct_deploy, direct_alice, direct_bob
    ):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, SUPPORTED_PAGE)
        mock_verdict(direct_vm, "FETCHED-PAGE ClaimWatch docs", "supported", unmet=[2])

        direct_vm.sender = direct_bob
        with direct_vm.expect_revert("cannot name a missing requirement"):
            contract.run_check("watch-1")
        assert contract.get_watch("watch-1")["checks"] == "0"

    def test_a_requirement_number_that_does_not_exist_is_refused(
        self, direct_vm, direct_deploy, direct_alice, direct_bob
    ):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", unmet=[9], material=True)

        direct_vm.sender = direct_bob
        with direct_vm.expect_revert("does not exist in this claim"):
            contract.run_check("watch-1")
        assert contract.get_watch("watch-1")["checks"] == "0"

    def test_an_unmet_value_that_is_not_a_number_is_refused(
        self, direct_vm, direct_deploy, direct_alice, direct_bob
    ):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        direct_vm.mock_llm(
            r"(?s).*not stored.*",
            json.dumps({"verdict": "contradicted", "unmet": ["first"], "material": True, "quote": "x", "reason": "y"}),
        )
        mock_page(direct_vm, CHANGED_PAGE)

        direct_vm.sender = direct_bob
        with direct_vm.expect_revert("not a number"):
            contract.run_check("watch-1")

    def test_the_history_keeps_unmet_and_material(self, direct_vm, direct_deploy, direct_alice, direct_bob):
        contract = deploy(direct_deploy)
        self.watch(contract, direct_vm, direct_alice)

        mock_page(direct_vm, CHANGED_PAGE)
        mock_verdict(direct_vm, "not stored", "contradicted", quote="rulings are not stored", unmet=[2], material=True)
        direct_vm.sender = direct_bob
        contract.run_check("watch-1")

        history = contract.checks_of("watch-1")
        assert history[0]["unmet"] == [2]
        assert history[0]["material"] is True
