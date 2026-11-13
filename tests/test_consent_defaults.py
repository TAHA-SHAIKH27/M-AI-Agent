"""Fresh installs must never inherit pre-granted permissions."""
from m_core.security.consent import Capability, ConsentManager


def test_fresh_consent_store_starts_empty_and_persists_explicit_grants(tmp_path):
    manager = ConsentManager(tmp_path)

    assert manager.is_granted(Capability.FILE_READ) is False

    manager.grant(Capability.FILE_READ)
    assert (tmp_path / "consents.json").is_file()
    assert manager.is_granted(Capability.FILE_READ) is True

    reloaded = ConsentManager(tmp_path)
    assert reloaded.is_granted(Capability.FILE_READ) is True
