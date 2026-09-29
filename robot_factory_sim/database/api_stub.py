"""업체 서버 연동용 REST API 클라이언트 스텁.

현재는 외부 네트워크 요청 없이 로컬 DB를 대상으로 동작한다.
실제 연동 시 base_url 기반 HTTP 구현으로 교체한다 (agent.md 5: 외부 요청은 사용자 동의 후).
"""

from __future__ import annotations

from typing import Any

from database.db_manager import DBManager
from utils.config_models import FactoryConfig


class ConfigAPIClient:
    """업체 서버에서 설정을 내려받을 때 사용하는 클라이언트 스텁."""

    def __init__(self, db: DBManager, base_url: str | None = None) -> None:
        """클라이언트를 생성한다.

        Args:
            db: 로컬 DBManager (오프라인 모드 동작용)
            base_url: 업체 서버 URL (미지정 시 오프라인 모드)
        """
        self._db = db
        self.base_url = base_url

    @property
    def is_offline(self) -> bool:
        """오프라인(로컬) 모드 여부."""
        return self.base_url is None

    def fetch_latest_config(self, factory_id: str) -> FactoryConfig:
        """최신 공장 구성을 가져온다. 오프라인 모드에서는 로컬 DB 구성을 반환한다.

        Args:
            factory_id: 업체 공장 ID

        Returns:
            FactoryConfig
        """
        if not self.is_offline:
            raise NotImplementedError(f"원격 API 연동은 아직 구현되지 않았습니다: {self.base_url} ({factory_id})")
        return self._db.load_factory_config()

    def push_config_update(self, factory_id: str, changes: dict[str, Any]) -> bool:
        """변경 사항을 업체 서버에 전송한다. 오프라인 모드에서는 전송하지 않는다.

        Args:
            factory_id: 업체 공장 ID
            changes: 변경 내용

        Returns:
            전송 성공 여부 (오프라인이면 False)
        """
        if self.is_offline:
            return False
        raise NotImplementedError(f"원격 API 연동은 아직 구현되지 않았습니다: {self.base_url} ({factory_id}, {changes})")
