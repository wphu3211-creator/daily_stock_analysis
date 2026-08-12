"""System configuration endpoints."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.deps import get_runtime_scheduler_service, get_system_config_service
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.system_config import (
    AgentBackendStatusPreviewRequest,
    AgentBackendStatusResponse,
    DiscoverLLMChannelModelsRequest,
    DiscoverLLMChannelModelsResponse,
    ExportSystemConfigResponse,
    GenerationBackendStatusPreviewRequest,
    GenerationBackendStatusResponse,
    ImportSystemConfigRequest,
    SystemConfigConflictResponse,
    SystemConfigResponse,
    SystemConfigSchemaResponse,
    SetupStatusResponse,
    TestGenerationBackendRequest,
    TestGenerationBackendResponse,
    SystemConfigValidationErrorResponse,
    TestLLMChannelRequest,
    TestLLMChannelResponse,
    TestNotificationChannelRequest,
    TestNotificationChannelResponse,
    UpdateSystemConfigRequest,
    UpdateSystemConfigResponse,
    ValidateSystemConfigRequest,
    ValidateSystemConfigResponse,
)
from src.auth import COOKIE_NAME, is_auth_enabled, refresh_auth_state, verify_session
from src.services.system_config_service import (
    ConfigConflictError,
    ConfigImportError,
    ConfigValidationError,
    SystemConfigService,
)
from src.services.runtime_scheduler import RuntimeSchedulerService

logger = logging.getLogger(__name__)

router = APIRouter()



# ---------------------------------------------------------------------------
# /market_indices — 主要指数实时行情 (Portal topbar 依赖)
# ---------------------------------------------------------------------------
# code → key 映射 (前端用 key 做 DOM 匹配)
_CODE_KEY_MAP = {
    # CN
    "sh000001": "shanghai",
    "sz399001": "shenzhen",
    "sz399006": "chinext",
    "sh000688": "star50",
    "sh000016": "sse50",
    "sh000300": "csi300",
    # HK
    "HSI": "hsi",
    "HSCEI": "hscei",
    "HSTECH": "hstech",
    # US
    "int_dji": "dji",
    "int_nasdaq": "nasdaq",
    "int_sp500": "sp500",
}


def _fetch_cn_indices() -> list[dict]:
    """Fetch CN A-share indices via akshare (sina source)."""
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_sina()
        indices_map = {
            'sh000001': '上证指数',
            'sz399001': '深证成指',
            'sz399006': '创业板指',
            'sh000688': '科创50',
            'sh000016': '上证50',
            'sh000300': '沪深300',
        }
        results = []
        for code, name in indices_map.items():
            row = df[df['代码'] == code]
            if row.empty:
                row = df[df['代码'].str.contains(code)]
            if not row.empty:
                r = row.iloc[0]
                current = float(r.get('最新价', 0) or 0)
                prev_close = float(r.get('昨收', 0) or 0)
                results.append({
                    'code': code,
                    'key': _CODE_KEY_MAP.get(code, code),
                    'name': name,
                    'region': 'cn',
                    'current': current,
                    'change': float(r.get('涨跌额', 0) or 0),
                    'change_pct': float(r.get('涨跌幅', 0) or 0),
                    'open': float(r.get('今开', 0) or 0),
                    'high': float(r.get('最高', 0) or 0),
                    'low': float(r.get('最低', 0) or 0),
                    'prev_close': prev_close,
                    'volume': float(r.get('成交量', 0) or 0),
                    'amount': float(r.get('成交额', 0) or 0),
                })
        return results
    except Exception as e:
        logger.warning("CN indices fetch failed: %s", e)
        return []


def _fetch_hk_indices() -> list[dict]:
    """Fetch HK indices via akshare (sina source)."""
    try:
        import akshare as ak
        df = ak.stock_hk_index_spot_sina()
        indices_map = {
            'HSI': '恒生指数',
            'HSCEI': '国企指数',
            'HSTECH': '恒生科技',
        }
        results = []
        for code, name in indices_map.items():
            row = df[df['代码'] == code]
            if not row.empty:
                r = row.iloc[0]
                current = float(r.get('最新价', 0) or 0)
                prev_close = float(r.get('昨收', 0) or 0)
                change = float(r.get('涨跌额', 0) or 0)
                change_pct = float(r.get('涨跌幅', 0) or 0)
                results.append({
                    'code': code,
                    'key': _CODE_KEY_MAP.get(code, code),
                    'name': name,
                    'region': 'hk',
                    'current': current,
                    'change': change,
                    'change_pct': change_pct,
                    'open': float(r.get('今开', 0) or 0),
                    'high': float(r.get('最高', 0) or 0),
                    'low': float(r.get('最低', 0) or 0),
                    'prev_close': prev_close,
                    'volume': 0,
                    'amount': 0,
                })
        return results
    except Exception as e:
        logger.warning("HK indices fetch failed: %s", e)
        return []


def _fetch_us_indices() -> list[dict]:
    """Fetch US indices via sina finance API directly."""
    import requests
    indices_map = {
        'int_dji': '道琼斯',
        'int_nasdaq': '纳斯达克',
        'int_sp500': '标普500',
    }
    symbols = ','.join(indices_map.keys())
    url = f'https://hq.sinajs.cn/list={symbols}'
    headers = {'Referer': 'https://finance.sina.com.cn'}
    results = []
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        # Parse response: var hq_str_int_dji="道琼斯,46247.29,299.97,0.65";
        for line in resp.text.strip().split('\n'):
            if '=' not in line:
                continue
            var_part, val_part = line.split('=', 1)
            symbol = var_part.split('_')[-1]
            # Reconstruct full symbol
            full_symbol = f'int_{symbol}' if not symbol.startswith('int_') else symbol
            # Actually the format is: var hq_str_int_dji=...
            # So we need to extract from var name
            sym = var_part.replace('var hq_str_', '')
            vals = val_part.strip('";').split(',')
            if len(vals) >= 4:
                name = vals[0]
                current = float(vals[1] or 0)
                change = float(vals[2] or 0)
                change_pct = float(vals[3] or 0)
                results.append({
                    'code': sym,
                    'key': _CODE_KEY_MAP.get(sym, sym),
                    'name': name,
                    'region': 'us',
                    'current': current,
                    'change': change,
                    'change_pct': change_pct,
                    'open': 0,
                    'high': 0,
                    'low': 0,
                    'prev_close': current - change if change else 0,
                    'volume': 0,
                    'amount': 0,
                })
    except Exception as e:
        logger.warning("US indices fetch failed: %s", e)
    return results


@router.get(
    "/market_indices",
    summary="Get major market indices",
    description="Return real-time major market index data (CN + HK + US) for Portal topbar.",
)
async def get_market_indices():
    """Fetch indices from CN, HK, and US markets."""
    from datetime import datetime, timezone
    
    all_indices = []
    all_indices.extend(_fetch_cn_indices())
    all_indices.extend(_fetch_hk_indices())
    all_indices.extend(_fetch_us_indices())
    
    if not all_indices:
        raise HTTPException(status_code=503, detail="No market index data available")
    
    return {
        "indices": all_indices,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "akshare+sina",
    }




@router.get(
    "/scheduler/status",
    summary="Get runtime scheduler status",
    description="Return status for the in-process Web/API/Desktop scheduler.",
)
def get_scheduler_status(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
) -> dict:
    """Return runtime scheduler status."""
    return scheduler.status()


@router.post(
    "/scheduler/run-now",
    summary="Run scheduled analysis now",
    description="Trigger one scheduled analysis run in the current process.",
)
def run_scheduler_now(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
) -> dict:
    """Trigger one runtime scheduled analysis run."""
    result = scheduler.run_now()
    if not result.get("accepted", False):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "scheduler_busy",
                "message": "A scheduled analysis is already running",
                "reason": result.get("reason", "analysis_already_running"),
            },
        )
    return result


class EnvBackupAccessDenied(Exception):
    """Raised when raw `.env` backup access is not allowed for this request."""

    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _allow_env_backup_access(request: Request) -> None:
    """Gate raw .env backup/restore to explicit secure modes.

    - Desktop runtime keeps existing local behavior via DSA_DESKTOP_MODE.
    - Non-desktop runtime must have admin auth enabled and a valid session.
    """
    if os.getenv("DSA_DESKTOP_MODE") == "true":
        return

    refresh_auth_state()
    if not is_auth_enabled():
        raise EnvBackupAccessDenied(
            status_code=403,
            message="System config backup is disabled; enable admin authentication first",
        )

    cookie_val = request.cookies.get(COOKIE_NAME)
    if cookie_val and verify_session(cookie_val):
        return

    raise EnvBackupAccessDenied(
        status_code=401,
        message="System config backup requires a valid admin session",
    )


def _raise_env_backup_access_error(exc: EnvBackupAccessDenied) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "error": "env_backup_access_denied",
            "message": exc.message,
        },
    )


@router.get(
    "/config",
    response_model=SystemConfigResponse,
    responses={
        200: {"description": "Configuration loaded"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get system configuration",
    description=(
        "Read current configuration and return display values. Server-masked "
        "sensitive fields may return the mask token; clients should use "
        "raw_value_exists and is_masked to interpret values."
    ),
)
def get_system_config(
    include_schema: bool = Query(True, description="Whether to include schema metadata"),
    service: SystemConfigService = Depends(get_system_config_service),
) -> SystemConfigResponse:
    """Load and return current system configuration."""
    try:
        payload = service.get_config(include_schema=include_schema)
        return SystemConfigResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load system configuration",
            },
        )


@router.get(
    "/config/setup/status",
    response_model=SetupStatusResponse,
    responses={
        200: {"description": "Setup status loaded"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get first-run setup status",
    description="Read a side-effect-free setup readiness summary from saved and runtime configuration.",
)
def get_setup_status(
    service: SystemConfigService = Depends(get_system_config_service),
) -> SetupStatusResponse:
    """Return first-run setup status without writing config or reloading runtime state."""
    try:
        payload = service.get_setup_status()
        return SetupStatusResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load setup status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load setup status",
            },
        )


@router.get(
    "/config/generation-backends/status",
    response_model=GenerationBackendStatusResponse,
    responses={
        200: {"description": "Generation backend status loaded"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get generation backend status",
    description=(
        "Read a side-effect-free generation backend cheap-check status from "
        "saved and runtime configuration. This endpoint does not run a model request."
    ),
)
def get_generation_backend_status(
    service: SystemConfigService = Depends(get_system_config_service),
) -> GenerationBackendStatusResponse:
    """Return saved/runtime generation backend status without writing config."""
    try:
        payload = service.get_generation_backend_status()
        return GenerationBackendStatusResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load generation backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load generation backend status",
            },
        )


@router.post(
    "/config/generation-backends/status/preview",
    response_model=GenerationBackendStatusResponse,
    responses={
        200: {"description": "Generation backend status preview loaded"},
        400: {"description": "Validation failed", "model": SystemConfigValidationErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Preview generation backend status",
    description="Run a side-effect-free cheap check against unsaved settings draft values.",
)
def preview_generation_backend_status(
    request: GenerationBackendStatusPreviewRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> GenerationBackendStatusResponse:
    """Return generation backend status for unsaved draft values."""
    try:
        payload = service.preview_generation_backend_status(
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
        )
        return GenerationBackendStatusResponse.model_validate(payload)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except Exception as exc:
        logger.error("Failed to preview generation backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to preview generation backend status",
            },
        )


@router.post(
    "/config/generation-backends/smoke-test",
    response_model=TestGenerationBackendResponse,
    responses={
        200: {"description": "Generation backend smoke test completed"},
        400: {"description": "Validation failed", "model": SystemConfigValidationErrorResponse},
        422: {"description": "Invalid smoke test request", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Smoke test generation backend",
    description="Run an explicit fixed-prompt generation backend smoke test without persisting config.",
)
def test_generation_backend(
    request: TestGenerationBackendRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> TestGenerationBackendResponse:
    """Run a fixed generation backend smoke test."""
    try:
        payload = service.test_generation_backend(
            backend_id=request.backend_id,
            mode=request.mode,
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
            timeout_seconds=request.timeout_seconds,
        )
        return TestGenerationBackendResponse.model_validate(payload)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to smoke test generation backend: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to smoke test generation backend",
            },
        )


@router.get(
    "/config/agent-backends/status",
    response_model=AgentBackendStatusResponse,
    summary="Get Agent Chat backend status",
    description="Read selected Agent Chat backend configuration and command capability without a model request.",
)
def get_agent_backend_status(
    service: SystemConfigService = Depends(get_system_config_service),
) -> AgentBackendStatusResponse:
    try:
        return AgentBackendStatusResponse.model_validate(service.get_agent_backend_status())
    except Exception as exc:
        logger.error("Failed to load Agent backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "Failed to load Agent backend status"},
        )


@router.post(
    "/config/agent-backends/status/preview",
    response_model=AgentBackendStatusResponse,
    summary="Preview Agent Chat backend status",
    description="Run a side-effect-free cheap check against unsaved Agent settings.",
)
def preview_agent_backend_status(
    request: AgentBackendStatusPreviewRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> AgentBackendStatusResponse:
    try:
        return AgentBackendStatusResponse.model_validate(
            service.preview_agent_backend_status(
                items=[item.model_dump() for item in request.items],
                mask_token=request.mask_token,
            )
        )
    except Exception as exc:
        logger.error("Failed to preview Agent backend status: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "Failed to preview Agent backend status"},
        )


@router.put(
    "/config",
    response_model=UpdateSystemConfigResponse,
    responses={
        200: {"description": "Configuration updated"},
        400: {"description": "Validation failed", "model": SystemConfigValidationErrorResponse},
        409: {"description": "Version conflict", "model": SystemConfigConflictResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Update system configuration",
    description="Update key-value pairs in .env. Mask token preserves existing secret values.",
)
def update_system_config(
    request: UpdateSystemConfigRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> UpdateSystemConfigResponse:
    """Validate and persist system configuration updates."""
    try:
        payload = service.update(
            config_version=request.config_version,
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
            reload_now=request.reload_now,
        )
        return UpdateSystemConfigResponse.model_validate(payload)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except ConfigConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "config_version_conflict",
                "message": "Configuration has changed, please reload and retry",
                "current_config_version": exc.current_version,
            },
        )
    except Exception as exc:
        logger.error("Failed to update system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to update system configuration",
            },
        )


@router.get(
    "/config/export",
    response_model=ExportSystemConfigResponse,
    responses={
        200: {"description": "Env exported"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Env backup disabled", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Export env backup",
    description="Return the raw saved .env content for configuration backup.",
)
def export_system_config(
    request: Request,
    service: SystemConfigService = Depends(get_system_config_service),
) -> ExportSystemConfigResponse:
    """Export the active `.env` file for config backup."""
    try:
        _allow_env_backup_access(request)
    except EnvBackupAccessDenied as exc:
        logger.warning("System config export blocked: %s", exc)
        _raise_env_backup_access_error(exc)

    try:
        payload = service.export_env()
        return ExportSystemConfigResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to export system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to export system configuration",
            },
        )


@router.post(
    "/config/import",
    response_model=UpdateSystemConfigResponse,
    responses={
        200: {"description": "Env imported"},
        400: {
            "description": "Import failed",
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/ErrorResponse"},
                            {"$ref": "#/components/schemas/SystemConfigValidationErrorResponse"},
                        ]
                    }
                }
            },
        },
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Env backup disabled", "model": ErrorResponse},
        409: {"description": "Version conflict", "model": SystemConfigConflictResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Import env backup",
    description="Merge raw .env text into the saved configuration with config version conflict protection.",
)
def import_system_config(
    request: ImportSystemConfigRequest,
    request_obj: Request,
    service: SystemConfigService = Depends(get_system_config_service),
) -> UpdateSystemConfigResponse:
    """Import a `.env` backup into the active config."""
    try:
        _allow_env_backup_access(request_obj)
    except EnvBackupAccessDenied as exc:
        logger.warning("System config import blocked: %s", exc)
        _raise_env_backup_access_error(exc)

    try:
        payload = service.import_env(
            config_version=request.config_version,
            content=request.content,
            reload_now=request.reload_now,
        )
        return UpdateSystemConfigResponse.model_validate(payload)
    except ConfigImportError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_import_file",
                "message": exc.message,
            },
        )
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "message": "System configuration validation failed",
                "issues": exc.issues,
            },
        )
    except ConfigConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "config_version_conflict",
                "message": "Configuration has changed, please reload and retry",
                "current_config_version": exc.current_version,
            },
        )
    except Exception as exc:
        logger.error("Failed to import system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to import system configuration",
            },
        )


@router.post(
    "/config/validate",
    response_model=ValidateSystemConfigResponse,
    responses={
        200: {"description": "Validation completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Validate system configuration",
    description="Validate submitted configuration values without writing to .env.",
)
def validate_system_config(
    request: ValidateSystemConfigRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> ValidateSystemConfigResponse:
    """Run pre-save validation only."""
    try:
        payload = service.validate(items=[item.model_dump() for item in request.items])
        return ValidateSystemConfigResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to validate system configuration: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to validate system configuration",
            },
        )


@router.post(
    "/config/llm/test-channel",
    response_model=TestLLMChannelResponse,
    responses={
        200: {"description": "Channel test completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Test one LLM channel",
    description="Run a minimal LLM request against one unsaved or saved channel definition.",
)
def test_llm_channel(
    request: TestLLMChannelRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> TestLLMChannelResponse:
    """Validate and test one channel definition without writing `.env`."""
    try:
        payload = service.test_llm_channel(
            name=request.name,
            protocol=request.protocol,
            api_surface=request.api_surface,
            base_url=request.base_url,
            api_key=request.api_key,
            models=request.models,
            enabled=request.enabled,
            timeout_seconds=request.timeout_seconds,
            capability_checks=request.capability_checks,
            use_saved_secret=request.use_saved_secret,
        )
        return TestLLMChannelResponse.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to test LLM channel: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to test LLM channel",
            },
        )


@router.post(
    "/config/notification/test-channel",
    response_model=TestNotificationChannelResponse,
    responses={
        200: {"description": "Notification channel test completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Test one notification channel",
    description="Send a short test notification using unsaved or saved notification configuration.",
)
def test_notification_channel(
    request: TestNotificationChannelRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> TestNotificationChannelResponse:
    """Validate and test one notification channel without writing `.env`."""
    try:
        payload = service.test_notification_channel(
            channel=request.channel,
            items=[item.model_dump() for item in request.items],
            mask_token=request.mask_token,
            title=request.title,
            content=request.content,
            timeout_seconds=request.timeout_seconds,
        )
        return TestNotificationChannelResponse.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to test notification channel: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to test notification channel",
            },
        )


@router.post(
    "/config/llm/discover-models",
    response_model=DiscoverLLMChannelModelsResponse,
    responses={
        200: {"description": "Model discovery completed"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Discover models for one LLM channel",
    description="Call one unsaved or saved channel's `/models` endpoint and return discovered model IDs.",
)
def discover_llm_channel_models(
    request: DiscoverLLMChannelModelsRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> DiscoverLLMChannelModelsResponse:
    """Discover models for one channel definition without writing `.env`."""
    try:
        payload = service.discover_llm_channel_models(
            name=request.name,
            protocol=request.protocol,
            base_url=request.base_url,
            api_key=request.api_key,
            models=request.models,
            timeout_seconds=request.timeout_seconds,
            use_saved_secret=request.use_saved_secret,
        )
        return DiscoverLLMChannelModelsResponse.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error("Failed to discover LLM channel models: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to discover LLM channel models",
            },
        )


@router.get(
    "/config/schema",
    response_model=SystemConfigSchemaResponse,
    responses={
        200: {"description": "Schema loaded"},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get system configuration schema",
    description="Return categorized field metadata used for dynamic settings form rendering.",
)
def get_system_config_schema(
    service: SystemConfigService = Depends(get_system_config_service),
) -> SystemConfigSchemaResponse:
    """Return schema metadata for system configuration fields."""
    try:
        payload = service.get_schema()
        return SystemConfigSchemaResponse.model_validate(payload)
    except Exception as exc:
        logger.error("Failed to load system configuration schema: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to load system configuration schema",
            },
        )
