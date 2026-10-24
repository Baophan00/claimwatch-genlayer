"""Direct-mode tests for ClaimWatch.

    python -m pytest tests/direct -q

The genlayer-test harness mocks the web and the model, runs `run_check` as the
leader, and can replay the consensus callback as a validator, so the page each
side read can be inspected rather than assumed.
"""

import json

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


def mock_verdict(direct_vm, marker, verdict, quote="rulings are stored on chain"):
    direct_vm.mock_llm(
        rf"(?s).*{marker}.*",
        json.dumps({"verdict": verdict, "quote": quote, "reason": f"page said {marker}"}),
    )


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
