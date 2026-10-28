# Gates: unified account pool

OWNS: mobius/**, tests/**, README.md, GATES.md

Scope: Claude와 Codex 자격을 사용자에게 하나의 계정 풀로 제공하고 capture/list/switch/fallback 경로를 검증한다.

- [ ] G1: 기존 Claude accounts.json이 통합 계정 모델로 무손실 로드된다
  CHECK: poetry run pytest -q tests/test_models.py tests/test_store.py && echo UNIFIED_G1_OK
  EXPECT: /UNIFIED_G1_OK/
  EVIDENCE: pending

- [ ] G2: 하나의 계정에 Claude와 Codex 자격을 함께 저장하고 다시 읽을 수 있다
  CHECK: poetry run pytest -q tests/test_unified_accounts.py && echo UNIFIED_G2_OK
  EXPECT: /UNIFIED_G2_OK/
  EVIDENCE: pending

- [ ] G3: 하나의 계정 목록에서 capture/list/switch가 Claude와 Codex 자격을 함께 처리한다
  CHECK: poetry run pytest -q tests/test_cli_codex.py tests/test_switcher.py && echo UNIFIED_G3_OK
  EXPECT: /UNIFIED_G3_OK/
  EVIDENCE: pending

- [ ] G4: 기존 Claude 자동 fallback과 전체 회귀가 유지된다
  CHECK: poetry run pytest -q && echo UNIFIED_G4_OK
  EXPECT: /UNIFIED_G4_OK/
  EVIDENCE: pending

- [ ] G5: 제공된 Codex 플러그인이 읽는 로컬 인증 상태와 switch 결과가 연결된다
  EVIDENCE: pending
