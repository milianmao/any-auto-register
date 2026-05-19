from core.local_ms_mailbox import LocalMicrosoftMailboxPool, parse_xinlan_common_rows


def test_parse_xinlan_common_rows_supports_four_field_graph_format():
    text = (
        "user@example.com----secret123----client-id-123----refresh-token-456\n"
    )

    rows = parse_xinlan_common_rows(text)

    assert len(rows) == 1
    row = rows[0]
    assert row.email == "user@example.com"
    assert row.password == "secret123"
    assert row.login_account == "user@example.com"
    assert row.client_id == "client-id-123"
    assert row.refresh_token == "refresh-token-456"
    assert row.graph_ready is True
    assert row.imap_ready is False
    assert row.imap_host == ""


def test_local_ms_pool_get_email_marks_four_field_graph_entry_as_graph_credentials(tmp_path):
    mailbox = LocalMicrosoftMailboxPool(
        pool_text="user@example.com----secret123----client-id-123----refresh-token-456",
        state_file=str(tmp_path / "local-ms-pool-state.json"),
    )

    account = mailbox.get_email()
    credentials = account.extra["provider_account"]["credentials"]

    assert account.email == "user@example.com"
    assert credentials["email"] == "user@example.com"
    assert credentials["password"] == "secret123"
    assert credentials["client_id"] == "client-id-123"
    assert credentials["refresh_token"] == "refresh-token-456"
    assert "imap_host" not in credentials
