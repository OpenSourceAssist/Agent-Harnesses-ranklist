import generate


def test_archived_is_dict_of_dates():
    assert isinstance(generate.ARCHIVED, dict)
    for gid, since in generate.ARCHIVED.items():
        assert "/" in gid
        assert len(since) == 10 and since[4] == "-"  # YYYY-MM-DD


def test_use_case_picks_skip_archived_repos():
    # KEEP_DESPITE_ARCHIVED keeps a repo in the ranked tables for reference,
    # but an archived repo must never be recommended as a use-case pick.
    gid = "FlowiseAI/Flowise"
    assert gid in generate.ARCHIVED and gid in generate.KEEP_DESPITE_ARCHIVED
    assert generate.is_recommendable(gid) is False
    for _, ids, _ in generate.USE_CASES:
        picks = [g for g in ids if generate.is_recommendable(g)]
        assert not set(picks) & set(generate.ARCHIVED)
    assert not any("Flowise" in item["a"] for item in generate.build_faq() if item["kind"] == "use-case")


def test_kept_archived_rows_are_labeled():
    readme = (generate.REPO_ROOT / "README.md").read_text()
    row = next(l for l in readme.splitlines() if '<a name="flowise"></a>' in l)
    assert "archived upstream" in row
