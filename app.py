#!/usr/bin/env python3
"""三月七助手 (March7thAssistant) 网页面板 v2。

- 状态/日志：解析 /m7a/logs 日志（可按日期查看）
- 容器控制：经 docker.sock 启停/重启主容器
- 单项任务：停主容器 -> 按主容器规格克隆一次性容器跑 `main.py <task>`
  （AFTER_FINISH=Exit）-> 跑完删一次性容器、恢复主容器；状态持久化、防重入
- 改配置：对 /m7a/config.yaml 原地（同 inode）手术式修改；字段由
  FIELD_SPECS 驱动（分组开关/数字/枚举）；after_finish 被镜像 ENV 锁死，
  改它时同步 compose 并按新环境重建主容器
- 体力计划 power_plan：YAML 编辑，校验后写入
"""
import http.client
import io
import json
import os
import re
import shutil
import socket
import tarfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

M7A_DIR = os.environ.get("M7A_DIR", "/m7a")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
LOG_DIR = os.path.join(M7A_DIR, "logs")
CONFIG_PATH = os.path.join(M7A_DIR, "config.yaml")
COMPOSE_PATH = os.path.join(M7A_DIR, "docker-compose.yml")
CONTAINER = "m7a"
ONESHOT = "m7a-oneshot"
DOCKER_SOCK = "/var/run/docker.sock"
JOB_PATH = os.path.join(DATA_DIR, "job.json")
UPDATE_PATH = os.path.join(DATA_DIR, "update.json")
PATCH_HOST_FALLBACK = "/opt/march7thassistant/patches/cloud.py"

INSTANCE_TYPES = ["拟造花萼（金）", "拟造花萼（赤）", "凝滞虚影", "侵蚀隧洞", "饰品提取"]
AFTER_FINISH_MODES = ["Loop", "None", "Exit"]

TASKS = [
    ("routine", "日常（一键全套）"),
    ("daily", "每日实训"),
    ("power", "清体力"),
    ("redemption", "兑换码"),
    ("divergent", "差分宇宙"),
    ("universe", "模拟宇宙"),
    ("currencywars", "货币战争"),
    ("fight", "锄大地"),
    ("forgottenhall", "混沌回忆"),
    ("purefiction", "虚构叙事"),
    ("apocalyptic", "末日幻影"),
    ("screen_test", "界面可切换性测试"),
    ("character_export", "角色面板导出"),
    ("notify", "测试消息推送"),
]
TASK_NAMES = dict(TASKS)

# (分组, key, 显示名, 类型, 选项)  类型: bool/int/text/enum
FIELD_SPECS = [
    ("云游戏", "cloud_game_use_paid_time", "允许使用付费时长", "bool", None),
    ("云游戏", "cloud_game_fullscreen_enable", "全屏运行", "bool", None),
    ("云游戏", "cloud_game_max_queue_time", "排队上限（分钟）", "int", None),
    ("云游戏", "cloud_game_login_timeout", "登录超时（分钟）", "int", None),
    ("体力与副本", "power_enable", "清体力总开关", "bool", None),
    ("体力与副本", "echo_of_war_enable", "历战余响（每周 3 次优先）", "bool", None),
    ("体力与副本", "use_reserved_trailblaze_power", "使用后备开拓力", "bool", None),
    ("体力与副本", "use_fuel", "使用燃料", "bool", None),
    ("体力与副本", "merge_immersifier", "优先合成沉浸器", "bool", None),
    ("体力与副本", "power_plan_keep", "体力计划执行后保留整份", "bool", None),
    ("体力与副本", "power_limit", "循环模式最低开拓力", "int", None),
    ("体力与副本", "loop_mode", "循环依据", "enum", ["power", "scheduled"]),
    ("体力与副本", "scheduled_time", "定时时间（loop_mode=scheduled 时）", "text", None),
    ("奖励领取", "reward_enable", "领取奖励总开关", "bool", None),
    ("奖励领取", "reward_dispatch_enable", "委托奖励", "bool", None),
    ("奖励领取", "reward_mail_enable", "邮件奖励", "bool", None),
    ("奖励领取", "reward_assist_enable", "支援奖励", "bool", None),
    ("奖励领取", "reward_quest_enable", "每日实训奖励", "bool", None),
    ("奖励领取", "reward_srpass_enable", "无名勋礼奖励", "bool", None),
    ("奖励领取", "reward_redemption_code_enable", "兑换码奖励", "bool", None),
    ("奖励领取", "reward_achievement_enable", "成就奖励", "bool", None),
    ("奖励领取", "reward_message_enable", "短信奖励", "bool", None),
    ("日常与活动", "daily_enable", "日常任务", "bool", None),
    ("日常与活动", "activity_enable", "活动功能总开关", "bool", None),
    ("日常与活动", "activity_gardenofplenty_enable", "花藏繁生活动", "bool", None),
    ("日常与活动", "activity_planarfissure_enable", "位面分裂活动", "bool", None),
    ("日常与活动", "activity_realmofthestrange_enable", "异器盈界活动", "bool", None),
    ("每周与挑战", "universe_enable", "模拟宇宙", "bool", None),
    ("每周与挑战", "weekly_divergent_enable", "差分宇宙积分奖励", "bool", None),
    ("每周与挑战", "currencywars_enable", "货币战争积分奖励", "bool", None),
    ("每周与挑战", "fight_enable", "锄大地", "bool", None),
    ("每周与挑战", "forgottenhall_enable", "混沌回忆", "bool", None),
    ("每周与挑战", "purefiction_enable", "虚构叙事", "bool", None),
    ("每周与挑战", "apocalyptic_enable", "末日幻影", "bool", None),
    ("每周与挑战", "forgottenhall_stellar_enable", "混沌回忆星启模式", "bool", None),
    ("每周与挑战", "purefiction_stellar_enable", "虚构叙事星启模式", "bool", None),
    ("每周与挑战", "apocalyptic_stellar_enable", "末日幻影星启模式", "bool", None),
    ("资产管理", "asset_manager_enable", "资产管理总开关", "bool", None),
    ("资产管理", "asset_ember_special_pass_enable", "每月自动买星轨专票", "bool", None),
    ("资产管理", "asset_ember_regular_pass_enable", "每月自动买星轨通票", "bool", None),
    ("资产管理", "asset_ember_tracks_of_destiny_enable", "每月自动买命运的足迹", "bool", None),
    ("资产管理", "asset_self_molding_resin_enable", "每月自动合成自塑尘脂", "bool", None),
]
SPEC_BY_KEY = {s[1]: s for s in FIELD_SPECS}

# v3 追加：支援 / 差分与模拟细项 / 货币战争 / 锄大地 / 培养与清理 / 对话与杂项 / 通知渠道
FIELD_SPECS += [
    ("支援角色", "borrow_enable", "使用支援角色", "bool", None),
    ("支援角色", "borrow_character_enable", "强制使用支援角色", "bool", None),
    ("支援角色", "borrow_scroll_times", "支援列表滚动查找次数", "int", None),
    ("差分宇宙细项", "divergent_station_priority_enable", "启用自定义站点优先级", "bool", None),
    ("差分宇宙细项", "weekly_divergent_type", "周常模式", "enum", ["normal", "cycle"]),
    ("差分宇宙细项", "weekly_divergent_level", "周常难度（1-6）", "int", None),
    ("差分宇宙细项", "weekly_divergent_bonus_enable", "领积分后自动饰品提取", "bool", None),
    ("差分宇宙细项", "weekly_divergent_stable_mode", "低性能兼容模式（云游戏强制开）", "bool", None),
    ("差分宇宙细项", "divergent_type", "单独执行模式", "enum", ["normal", "cycle"]),
    ("差分宇宙细项", "divergent_team_type", "队伍类型", "enum", ["追击", "dot", "终结技", "击破", "盾反"]),
    ("差分宇宙细项", "divergent_auto_save_enable", "通关自动存档到空位", "bool", None),
    ("模拟宇宙细项", "universe_category", "类别", "enum", ["divergent", "universe"]),
    ("模拟宇宙细项", "universe_frequency", "运行频率", "enum", ["weekly", "daily"]),
    ("模拟宇宙细项", "universe_count", "每次尝试运行次数", "int", None),
    ("模拟宇宙细项", "universe_bonus_enable", "自动领取沉浸奖励", "bool", None),
    ("模拟宇宙细项", "universe_difficulty", "难度（0=默认）", "int", None),
    ("模拟宇宙细项", "universe_fate", "命途", "text", None),
    ("货币战争细项", "currencywars_type", "模式", "enum", ["normal", "overclock"]),
    ("货币战争细项", "currencywars_rank_difficulty", "职级难度", "enum", ["current", "lowest", "highest"]),
    ("货币战争细项", "currencywars_strategy", "策略", "enum", ["default", "aglaea"]),
    ("货币战争细项", "currencywars_bonus_enable", "领积分后快速提取饰品", "bool", None),
    ("货币战争细项", "currencywars_fast_mode", "速通模式", "bool", None),
    ("货币战争细项", "currencywars_strategy_restart_on_special_tags", "特殊词条时接受重开", "bool", None),
    ("锄大地细项", "fight_team_enable", "自动切换队伍", "bool", None),
    ("锄大地细项", "fight_team_number", "队伍编号", "int", None),
    ("锄大地细项", "fight_reward_enable", "特殊物品领取", "bool", None),
    ("锄大地细项", "fight_allow_map_buy", "购买代币与过期邮包", "text", None),
    ("锄大地细项", "fight_allow_snack_buy", "购买秘技零食", "text", None),
    ("锄大地细项", "fight_main_map", "优先星球（0不配置 1空间站 2雅利洛 3仙舟 4匹诺康尼 5翁法罗斯）", "enum", ["0", "1", "2", "3", "4", "5"]),
    ("锄大地细项", "fight_map_version", "地图版本", "enum", ["不配置", "default", "HuangQuan", "reward"]),
    ("培养与清理", "build_target_enable", "启用培养目标", "bool", None),
    ("培养与清理", "build_target_scheme", "培养目标识别方案", "enum", ["instance", "drop"]),
    ("培养与清理", "build_target_ornament_weekly_count", "培养目标每周饰品提取次数（0-7）", "int", None),
    ("培养与清理", "weekly_relic_smart_discard_enable", "每周清理先智能弃置五星遗器", "bool", None),
    ("培养与清理", "weekly_relic_cleanup_day_of_week", "每周遗器清理起始日（1=周一）", "int", None),
    ("培养与清理", "instance_team_enable", "打副本自动切换队伍", "bool", None),
    ("培养与清理", "instance_team_number", "副本队伍编号", "int", None),
    ("培养与清理", "tp_before_instance", "进副本前先传送锚点", "bool", None),
    ("培养与清理", "calyx_golden_preference", "拟造花萼（金）偏好地区", "enum", ["Jarilo-VI", "XianzhouLuofu", "Penacony"]),
    ("对话与杂项", "autoplot_remember_enable", "记住自动对话开关状态", "bool", None),
    ("对话与杂项", "autoplot_skip_enable", "自动跳过对话", "bool", None),
    ("对话与杂项", "autoplot_click_enable", "自动选择对话选项", "bool", None),
    ("对话与杂项", "autoplot_battle_detect_enable", "自动战斗检测", "bool", None),
    ("对话与杂项", "autoplot_phone_detect_enable", "自动处理手机短信页面", "bool", None),
    ("对话与杂项", "autoplot_hero_chronicle_enable", "自动阅读英雄纪", "bool", None),
    ("对话与杂项", "check_update", "检查更新", "bool", None),
    ("对话与杂项", "log_level", "日志等级", "enum", ["INFO", "DEBUG"]),
    ("对话与杂项", "activity_dailycheckin_enable", "每日签到活动", "bool", None),
    ("对话与杂项", "activity_gardenofplenty_instance_type", "花藏繁生副本类型", "enum", INSTANCE_TYPES),
    # 通知渠道（secret = 密钥类，留空不改）
    ("通知 · 钉钉", "notify_dingtalk_enable", "启用", "bool", None),
    ("通知 · 钉钉", "notify_dingtalk_token", "Token", "secret", None),
    ("通知 · 钉钉", "notify_dingtalk_secret", "Secret", "secret", None),
    ("通知 · Pushplus", "notify_pushplus_enable", "启用", "bool", None),
    ("通知 · Pushplus", "notify_pushplus_token", "Token", "secret", None),
    ("通知 · Pushplus", "notify_pushplus_channel", "渠道", "text", None),
    ("通知 · Pushplus", "notify_pushplus_webhook", "Webhook", "text", None),
    ("通知 · Qmsg", "notify_qmsg_enable", "启用", "bool", None),
    ("通知 · Qmsg", "notify_qmsg_key", "Key", "secret", None),
    ("通知 · Qmsg", "notify_qmsg_qq", "接收 QQ 号", "text", None),
    ("通知 · Qmsg", "notify_qmsg_mode", "发送模式", "text", None),
    ("通知 · 企业微信应用", "notify_wechatworkapp_enable", "启用", "bool", None),
    ("通知 · 企业微信应用", "notify_wechatworkapp_corpid", "企业 ID", "text", None),
    ("通知 · 企业微信应用", "notify_wechatworkapp_corpsecret", "应用密钥", "secret", None),
    ("通知 · 企业微信应用", "notify_wechatworkapp_agentid", "AgentId", "text", None),
    ("通知 · 企业微信应用", "notify_wechatworkapp_touser", "目标用户", "text", None),
    ("通知 · 企业微信应用", "notify_wechatworkapp_base_url", "API 地址（可空）", "text", None),
    ("通知 · 企业微信机器人", "notify_wechatworkbot_enable", "启用", "bool", None),
    ("通知 · 企业微信机器人", "notify_wechatworkbot_key", "机器人 Key", "secret", None),
    ("通知 · 企业微信机器人", "notify_wechatworkbot_webhook_url", "Webhook URL（可空）", "text", None),
    ("通知 · Gotify", "notify_gotify_enable", "启用", "bool", None),
    ("通知 · Gotify", "notify_gotify_url", "服务器 URL", "text", None),
    ("通知 · Gotify", "notify_gotify_token", "Token", "secret", None),
    ("通知 · Gotify", "notify_gotify_priority", "优先级（1-10）", "int", None),
    ("通知 · Discord", "notify_discord_enable", "启用", "bool", None),
    ("通知 · Discord", "notify_discord_webhook", "Webhook", "text", None),
    ("通知 · Discord", "notify_discord_username", "用户名（可空）", "text", None),
    ("通知 · Discord", "notify_discord_avatar_url", "头像 URL（可空）", "text", None),
    ("通知 · Discord", "notify_discord_color", "嵌入颜色（可空）", "text", None),
    ("通知 · Pushdeer", "notify_pushdeer_enable", "启用", "bool", None),
    ("通知 · Pushdeer", "notify_pushdeer_token", "Token", "secret", None),
    ("通知 · Pushdeer", "notify_pushdeer_url", "服务 URL（可空）", "text", None),
    ("通知 · 飞书", "notify_lark_enable", "启用", "bool", None),
    ("通知 · 飞书", "notify_lark_webhook", "Webhook", "text", None),
    ("通知 · 飞书", "notify_lark_keyword", "关键词（可空）", "text", None),
    ("通知 · 飞书", "notify_lark_sign", "签名密钥", "secret", None),
    ("通知 · 飞书", "notify_lark_content", "内容模板（可空）", "text", None),
    ("通知 · 飞书", "notify_lark_appid", "应用 ID（发图时必填）", "text", None),
    ("通知 · 飞书", "notify_lark_secret", "应用密钥", "secret", None),
    ("通知 · 飞书", "notify_lark_imageenable", "启用图片信息发送", "bool", None),
    ("通知 · KOOK", "notify_kook_enable", "启用", "bool", None),
    ("通知 · KOOK", "notify_kook_token", "Token", "secret", None),
    ("通知 · KOOK", "notify_kook_target_id", "目标 ID", "text", None),
    ("通知 · KOOK", "notify_kook_chat_type", "消息类型（1私聊 9频道）", "text", None),
    ("通知 · Meow", "notify_meow_enable", "启用", "bool", None),
    ("通知 · Meow", "notify_meow_nickname", "昵称", "text", None),
    ("通知 · 通用 Webhook", "notify_webhook_enable", "启用", "bool", None),
    ("通知 · 通用 Webhook", "notify_webhook_url", "接收地址", "text", None),
    ("通知 · 通用 Webhook", "notify_webhook_method", "请求方法", "text", None),
    ("通知 · 通用 Webhook", "notify_webhook_headers", "Headers（JSON 字符串）", "text", None),
    ("通知 · 通用 Webhook", "notify_webhook_body", "请求体模板", "text", None),
    ("通知 · 自定义", "notify_custom_enable", "启用", "bool", None),
    ("通知 · 自定义", "notify_custom_url", "URL", "text", None),
    ("通知 · 自定义", "notify_custom_method", "请求类型（get/post）", "text", None),
    ("通知 · 自定义", "notify_custom_datatype", "数据类型（data/json）", "text", None),
    ("通知 · 自定义", "notify_custom_image", "图片参数（可空）", "text", None),
    ("通知 · 自定义", "notify_custom_data", "数据模板", "text", None),
]
SPEC_BY_KEY = {s[1]: s for s in FIELD_SPECS}

# v4 二期补全：与 Windows 端设置卡片对齐后漏掉的字段（时间戳/计数器与桌面专属项除外）
FIELD_SPECS += [
    ("云游戏", "cloud_game_enable", "启动云游戏", "bool", None),
    ("云游戏", "browser_type", "浏览器类型", "enum", ["integrated", "edge", "chrome"]),
    ("云游戏", "browser_headless_enable", "无窗口模式", "bool", None),
    ("云游戏", "browser_headless_restart_on_not_logged_in", "未登录时自动有窗口重启", "bool", None),
    ("云游戏", "browser_persistent_enable", "保存浏览器数据（登录态）", "bool", None),
    ("云游戏", "browser_download_use_mirror", "镜像下载浏览器", "bool", None),
    ("云游戏", "browser_dump_cookies_enable", "调试：转储 Cookies", "bool", None),
    ("云游戏", "browser_scale_factor", "浏览器缩放比", "float", None),
    ("云游戏", "start_game_timeout", "启动游戏超时（分）", "int", None),
    ("日常与活动", "daily_material_enable", "合成材料完成任务", "bool", None),
    ("日常与活动", "daily_himeko_try_enable", "姬子试用完成任务", "bool", None),
    ("日常与活动", "daily_memory_one_enable", "回忆一完成任务", "bool", None),
    ("日常与活动", "activity_journey_highlights_notification_enable", "活动热点通知", "bool", None),
    ("体力与副本", "echo_of_war_start_day_of_week", "历战余响起始日（1=周一）", "int", None),
    ("体力与副本", "exit_after_failure", "失败后直接退出", "bool", None),
    ("资产管理", "asset_lc3_star_superimpose_enable", "三星光锥自动叠加", "bool", None),
    ("培养与清理", "build_target_use_user_instance_when_only_erosion_and_ornament", "仅侵蚀/饰品时用手动副本", "bool", None),
    ("培养与清理", "break_down_level_four_relicset", "每周清理四星及以下遗器", "bool", None),
    ("货币战争细项", "currencywars_remembrance_trailblazer_name", "阿格莱雅策略记忆开拓者名", "text", None),
    ("锄大地细项", "fight_allow_memory_token", "获得翁法罗斯记忆代币", "text", None),
    ("锄大地细项", "fight_timeout", "锄大地超时（小时）", "int", None),
    ("模拟宇宙细项", "universe_timeout", "模拟宇宙超时（小时）", "int", None),
    ("模拟宇宙细项", "universe_enable_gpu", "GPU 加速", "bool", None),
    ("对话与杂项", "auto_battle_detect_enable", "自动战斗和二倍速检测", "bool", None),
    ("对话与杂项", "refresh_hour", "每日刷新时间（时）", "int", None),
    ("对话与杂项", "telemetry_enable", "匿名使用数据收集", "bool", None),
    ("通知总开关", "notification_enable", "启用消息推送", "bool", None),
    ("通知总开关", "notify_level", "通知级别", "enum", ["all", "error"]),
    ("通知总开关", "notify_merge", "合并通知", "bool", None),
    ("通知总开关", "notify_send_images", "推送时发送图片", "bool", None),
    ("通知 · Telegram", "notify_telegram_enable", "启用", "bool", None),
    ("通知 · Telegram", "notify_telegram_token", "Bot Token", "secret", None),
    ("通知 · Telegram", "notify_telegram_userid", "接收用户 ID", "text", None),
    ("通知 · Telegram", "notify_telegram_api_url", "自定义 API 地址（可空）", "text", None),
    ("通知 · Telegram", "notify_telegram_proxies", "代理（可空）", "text", None),
    ("通知 · Telegram", "notify_telegram_thread_id", "话题 ID（可空）", "text", None),
    ("通知 · SMTP", "notify_smtp_enable", "启用", "bool", None),
    ("通知 · SMTP", "notify_smtp_host", "SMTP 服务器", "text", None),
    ("通知 · SMTP", "notify_smtp_port", "端口", "text", None),
    ("通知 · SMTP", "notify_smtp_user", "用户名", "text", None),
    ("通知 · SMTP", "notify_smtp_password", "密码", "secret", None),
    ("通知 · SMTP", "notify_smtp_ssl", "SSL", "bool", None),
    ("通知 · SMTP", "notify_smtp_starttls", "STARTTLS", "bool", None),
    ("通知 · SMTP", "notify_smtp_ssl_unverified", "不验证 SSL 证书", "bool", None),
    ("通知 · SMTP", "notify_smtp_From", "发件人", "text", None),
    ("通知 · SMTP", "notify_smtp_To", "收件人", "text", None),
    ("通知 · SMTP", "notify_smtp_plain_text", "纯文本发送", "bool", None),
    ("通知 · Bark", "notify_bark_enable", "启用", "bool", None),
    ("通知 · Bark", "notify_bark_key", "Key", "secret", None),
    ("通知 · Bark", "notify_bark_url", "URL（可空）", "text", None),
    ("通知 · Bark", "notify_bark_base_url", "服务器地址（可空）", "text", None),
    ("通知 · Bark", "notify_bark_sound", "提示音（可空）", "text", None),
    ("通知 · Bark", "notify_bark_group", "分组（可空）", "text", None),
    ("通知 · Bark", "notify_bark_icon", "图标 URL（可空）", "text", None),
    ("通知 · Bark", "notify_bark_copy", "复制内容（可空）", "text", None),
    ("通知 · Bark", "notify_bark_autocopy", "自动复制（可空）", "text", None),
    ("通知 · Bark", "notify_bark_isarchive", "存档（可空）", "text", None),
    ("通知 · Bark", "notify_bark_cipherkey", "加密密钥（可空）", "secret", None),
    ("通知 · Bark", "notify_bark_ciphermethod", "加密方式（可空）", "text", None),
    ("通知 · Matrix", "notify_matrix_enable", "启用", "bool", None),
    ("通知 · Matrix", "notify_matrix_homeserver", "服务器地址", "text", None),
    ("通知 · Matrix", "notify_matrix_user_id", "用户 ID", "text", None),
    ("通知 · Matrix", "notify_matrix_device_id", "设备 ID（可空）", "text", None),
    ("通知 · Matrix", "notify_matrix_access_token", "Access Token", "secret", None),
    ("通知 · Matrix", "notify_matrix_room_id", "房间 ID", "text", None),
    ("通知 · Matrix", "notify_matrix_proxy", "代理（可空）", "text", None),
    ("通知 · Matrix", "notify_matrix_separately_text_media", "文本媒体分开发送", "bool", None),
    ("通知 · OneBot", "notify_onebot_enable", "启用", "bool", None),
    ("通知 · OneBot", "notify_onebot_endpoint", "上报地址", "text", None),
    ("通知 · OneBot", "notify_onebot_token", "Token（可空）", "secret", None),
    ("通知 · OneBot", "notify_onebot_user_id", "接收 QQ 号（可空）", "text", None),
    ("通知 · OneBot", "notify_onebot_group_id", "接收群号（可空）", "text", None),
    ("通知 · GoCQHTTP", "notify_gocqhttp_enable", "启用", "bool", None),
    ("通知 · GoCQHTTP", "notify_gocqhttp_endpoint", "上报地址", "text", None),
    ("通知 · GoCQHTTP", "notify_gocqhttp_token", "Token（可空）", "secret", None),
    ("通知 · GoCQHTTP", "notify_gocqhttp_user_id", "接收 QQ 号（可空）", "text", None),
    ("通知 · GoCQHTTP", "notify_gocqhttp_group_id", "接收群号（可空）", "text", None),
    ("通知 · GoCQHTTP", "notify_gocqhttp_message_type", "消息类型", "text", None),
    ("通知 · Server酱3", "notify_serverchan3_enable", "启用", "bool", None),
    ("通知 · Server酱3", "notify_serverchan3_sendkey", "SendKey", "secret", None),
    ("通知 · Server酱Turbo", "notify_serverchanturbo_enable", "启用", "bool", None),
    ("通知 · Server酱Turbo", "notify_serverchanturbo_sctkey", "SCTKey", "secret", None),
    ("通知 · Server酱Turbo", "notify_serverchanturbo_channel", "渠道（可空）", "text", None),
    ("通知 · Server酱Turbo", "notify_serverchanturbo_openid", "OpenID（可空）", "text", None),
    ("通知 · QQ 机器人", "notify_qqbot_enable", "启用", "bool", None),
    ("通知 · QQ 机器人", "notify_qqbot_appid", "机器人 AppID", "text", None),
    ("通知 · QQ 机器人", "notify_qqbot_client_secret", "Client Secret", "secret", None),
    ("通知 · QQ 机器人", "notify_qqbot_group_openid", "群 group_openid（可空）", "text", None),
    ("通知 · QQ 机器人", "notify_qqbot_openid", "单聊 openid（可空）", "text", None),
    ("更新", "update_source", "更新源", "enum", ["GitHub", "MirrorChyan"]),
    ("更新", "update_prerelease_enable", "加入预览版更新渠道", "bool", None),
    ("更新", "update_full_enable", "更新时下载完整包", "bool", None),
    ("更新", "update_download_proxy", "更新下载代理（可空）", "text", None),
    ("更新", "mirrorchyan_cdk", "MirrorChyan CDK", "secret", None),
]
SPEC_BY_KEY = {s[1]: s for s in FIELD_SPECS}

# YAML 结构化字段（名单/映射/范围），用 YAML 编辑器改，效果与桌面面板一致
YAML_FIELDS = [
    ("borrow_friends", "支援角色名单（[昵称, 角色] 列表）"),
    ("divergent_station_priority", "差分宇宙站点优先级（站点: 优先级）"),
    ("divergent_station_disabled", "差分宇宙禁用站点（列表）"),
    ("daily_tasks", "每日实训任务配置"),
    ("forgottenhall_level", "混沌回忆关卡范围"),
    ("purefiction_level", "虚构叙事关卡范围"),
    ("apocalyptic_level", "末日幻影关卡范围"),
    ("instance_teams", "副本自动队伍配置"),
]
YAML_FIELDS += [
    ("daily_memory_one_team", "回忆一队伍配置"),
    ("redemption_code", "兑换码列表"),
    ("notify_template", "通知模板"),
    ("browser_launch_argument", "浏览器启动额外参数"),
    ("forgottenhall_team1", "混沌回忆队伍 1"),
    ("forgottenhall_team2", "混沌回忆队伍 2"),
    ("purefiction_team1", "虚构叙事队伍 1"),
    ("purefiction_team2", "虚构叙事队伍 2"),
    ("apocalyptic_team1", "末日幻影队伍 1"),
    ("apocalyptic_team2", "末日幻影队伍 2"),
]
YAML_KEYS = {k for k, _ in YAML_FIELDS}

CHARACTER_NAMES = {
    'Acheron': '黄泉', 'Aglaea': '阿格莱雅', 'Anaxa': '那刻夏', 'Archer': 'Archer',
    'Argenti': '银枝', 'Arlan': '阿兰', 'Ashveil': '不死途', 'Asta': '艾丝妲',
    'Aventurine': '砂金', 'AventurineWaveflair': '砂金·戏浪', 'Bailu': '白露', 'BlackSwan': '黑天鹅',
    'Blade': '刃', 'Boothill': '波提欧', 'Bronya': '布洛妮娅', 'CaelumDestruction': '穹（毁灭）',
    'CaelumElation': '穹（欢愉）', 'CaelumImaginary': '穹（同谐）', 'CaelumPreservation': '穹（存护）', 'CaelumRemembrance': '穹（记忆）',
    'Castorice': '遐蝶', 'CastoriceGossamerFlutter': '遐蝶「幽梦翩跹」', 'Cerydra': '刻律德菈', 'Cipher': '赛飞儿',
    'Clara': '克拉拉', 'Cyrene': '昔涟', 'DanHeng': '丹恒', 'DanHengImbibitorLunae': '丹恒·饮月',
    'DanHengPermansorTerrae': '丹恒·腾荒', 'DrRatio': '真理医生', 'Evanescia': '绯英', 'EvanesciaLunarBlossoming': '绯英·月待花时',
    'Evernight': '长夜月', 'Feixiao': '飞霄', 'Firefly': '流萤', 'FireflySpringMissive': '流萤「春日手信」',
    'FuXuan': '符玄', 'Fugue': '忘归人', 'Gallagher': '加拉赫', 'Gepard': '杰帕德',
    'Gilgamesh': '吉尔伽美什', 'Guinaifen': '桂乃芬', 'Hanya': '寒鸦', 'Herta': '黑塔',
    'Himeko': '姬子', 'HimekoNova': '姬子·启行', 'Hook': '虎克', 'Huohuo': '藿藿',
    'Hyacine': '风堇', 'HyacineWarmCottonSkies': '风堇·云边拾暖', 'Hysilens': '海瑟音', 'Jade': '翡翠',
    'Jiaoqiu': '椒丘', 'JingYuan': '景元', 'Jingliu': '镜流', 'Kafka': '卡芙卡',
    'Lingsha': '灵砂', 'Luka': '卢卡', 'Luocha': '罗刹', 'Lynx': '玲可',
    'March7th': '三月七（冰）', 'March7thImaginary': '三月七（虚数）', 'March7thNascentSpring': '三月七「冬去煦至」', 'Misha': '米沙',
    'MortenaxBlade': '千冶·刃', 'Moze': '貊泽', 'Mydei': '万敌', 'Natasha': '娜塔莎',
    'Pearl': '真珠', 'Pela': '佩拉', 'Phainon': '白厄', 'Qingque': '青雀',
    'Rappa': '乱破', 'RinTohsaka': '远坂凛', 'Robin': '知更鸟', 'RobinSummeretto': '知更鸟·晴歌',
    'RuanMei': '阮·梅', 'RuanMeiPlumblossomLetter': '阮·梅「雪绽梅笺」', 'Saber': 'Saber', 'Sampo': '桑博',
    'Seele': '希儿', 'Serval': '希露瓦', 'SilverWolf': '银狼', 'SilverWolfLV999': '银狼LV.999',
    'Sparkle': '花火', 'Sparxie': '火花', 'SparxieCandyCoatedWaves': '火花·甜梦电波', 'StelleDestruction': '星（毁灭）',
    'StelleElation': '星（欢愉）', 'StelleImaginary': '星（同谐）', 'StellePreservation': '星（存护）', 'StelleRemembrance': '星（记忆）',
    'Sunday': '星期日', 'Sushang': '素裳', 'TheDahlia': '大丽花', 'TheHerta': '大黑塔',
    'Tingyun': '停云', 'TopazandNumby': '托帕&账账', 'Tribbie': '缇宝', 'Welt': '瓦尔特',
    'Xueyi': '雪衣', 'Yanqing': '彦卿', 'YaoGuang': '爻光', 'Yukong': '驭空',
    'Yunli': '云璃',
}

TECHNIQUES = [(-1, "秘技 / 开怪"), (0, "无操作"), (1, "秘技 1 次"), (2, "秘技 2 次")]
STATION_DEFAULTS = {"战斗": 1, "精英": 1, "事件": 2, "奖励": 2, "异常": 2, "铸造": 2}
STATION_FIXED = ["首领", "休整", "转化"]

_lock = threading.Lock()


# ---------- Docker over unix socket ----------
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


def docker_req(method, path, body=None, timeout=30):
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = raw.decode("utf-8", "replace")
    return resp.status, data


def docker_req_bytes(method, path, body=None, timeout=60):
    """返回原始字节（用于镜像拉取流、文件归档等非 JSON 响应）。"""
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def inspect(name):
    code, data = docker_req("GET", f"/containers/{name}/json")
    return data if code == 200 else None


def container_info():
    data = inspect(CONTAINER)
    if not data:
        return {"exists": False}
    state = data.get("State", {})
    env = data.get("Config", {}).get("Env", [])
    af_env = next((e.split("=", 1)[1] for e in env if e.startswith("MARCH7TH_AFTER_FINISH=")), None)
    return {
        "exists": True,
        "running": state.get("Running", False),
        "status": state.get("Status", ""),
        "started_at": state.get("StartedAt", ""),
        "image": data.get("Config", {}).get("Image", ""),
        "after_finish_env": af_env,
    }


def container_action(action, name=CONTAINER):
    code, data = docker_req("POST", f"/containers/{name}/{action}?t=15", timeout=90)
    if code not in (200, 204, 304):
        raise RuntimeError(f"docker {action} {name} -> {code}: {data}")
    return True


def clone_body(spec, env_repl=None, cmd=None, strip_compose_labels=True):
    cfg = spec["Config"]
    env = list(cfg.get("Env", []))
    for k, v in (env_repl or {}).items():
        env = [e for e in env if not e.startswith(k + "=")]
        env.append(f"{k}={v}")
    labels = cfg.get("Labels") or {}
    if strip_compose_labels:
        labels = {k: v for k, v in labels.items() if not k.startswith("com.docker.compose.")}
    host = dict(spec.get("HostConfig") or {})
    networks = list(spec.get("NetworkSettings", {}).get("Networks", {}).keys())
    return {
        "Image": cfg["Image"],
        "Env": env,
        "Cmd": cmd if cmd is not None else cfg.get("Cmd"),
        "Entrypoint": cfg.get("Entrypoint"),
        "Labels": labels,
        "WorkingDir": cfg.get("WorkingDir"),
        "HostConfig": host,
        "NetworkingConfig": {"EndpointsConfig": {n: {} for n in networks}} if networks else None,
    }


def recreate_with_after_finish(mode):
    spec = inspect(CONTAINER)
    if not spec:
        raise RuntimeError("读取容器规格失败")
    body = clone_body(spec, env_repl={"MARCH7TH_AFTER_FINISH": mode}, strip_compose_labels=False)
    was_running = spec.get("State", {}).get("Running", False)
    docker_req("POST", f"/containers/{CONTAINER}/stop?t=10", timeout=60)
    code, data = docker_req("DELETE", f"/containers/{CONTAINER}")
    if code not in (200, 204):
        raise RuntimeError(f"删除旧容器失败: {code} {data}")
    code, data = docker_req("POST", f"/containers/create?name={CONTAINER}", body=body)
    if code not in (200, 201):
        body_fb = clone_body(spec, strip_compose_labels=False)
        docker_req("POST", f"/containers/create?name={CONTAINER}", body=body_fb)
        raise RuntimeError(f"按新参数重建失败，已用原参数恢复: {code} {data}")
    if was_running:
        container_action("start")


# ---------- 镜像更新（含排队补丁自动重打） ----------
def load_update():
    try:
        with open(UPDATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"phase": "idle"}


def save_update(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = UPDATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, UPDATE_PATH)


def update_active(st=None):
    st = st or load_update()
    return st.get("phase") in ("pulling", "patching", "verifying", "installing", "recreating")


def image_info(ref):
    code, data = docker_req("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")
    return data if code == 200 else None


def split_image_ref(ref):
    if ":" in ref.rsplit("/", 1)[-1]:
        repo, tag = ref.rsplit(":", 1)
    else:
        repo, tag = ref, "latest"
    return repo, tag


def container_archive(name, path):
    """从容器文件系统中取一个文件，返回字节；取不到返回 None。"""
    code, raw = docker_req_bytes("GET", f"/containers/{name}/archive?path={urllib.parse.quote(path, safe='')}", timeout=120)
    if code != 200 or not raw:
        return None
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except Exception:
        return None
    base = path.rsplit("/", 1)[-1]
    for m in tf.getmembers():
        if m.isfile() and m.name.rsplit("/", 1)[-1] == base:
            return tf.extractfile(m).read()
    return None


def extract_from_image(image, path):
    name = f"m7a-tmp-{int(time.time())}"
    code, data = docker_req("POST", f"/containers/create?name={name}",
                            body={"Image": image, "Cmd": ["true"]})
    if code not in (200, 201):
        raise RuntimeError(f"创建临时容器失败: {code} {data}")
    try:
        blob = container_archive(name, path)
    finally:
        docker_req("DELETE", f"/containers/{name}?force=true")
    if blob is None:
        raise RuntimeError(f"无法从镜像中提取 {path}")
    return blob


def apply_cloud_patch(new_upstream, current_patched):
    """把排队补丁（3 处）重打到新版上游 cloud.py 上；锚点对不上就抛错（安全中止）。

    上游若已内置同款兜底，直接返回上游原文。
    """
    if "__m7aCloudPingFallbackInstalled" in new_upstream:
        return new_upstream, "upstream_builtin"
    m_start = current_patched.index("    def _install_cloud_ping_fallback")
    m_end = current_patched.index("\n    def _get_remaining_playtime", m_start)
    method_text = current_patched[m_start:m_end].rstrip("\n") + "\n"
    if "window.__m7aCloudPingFallbackInstalled" not in method_text:
        raise RuntimeError("现有补丁文件不完整，无法提取兜底方法")
    if "_install_cloud_ping_fallback" in new_upstream:
        raise RuntimeError("新版上游已有同名方法，补丁无法安全重打")
    out = new_upstream
    anchor1 = "            self._wait_game_page_loaded()\n\n    def _get_remaining_playtime"
    if out.count(anchor1) != 1:
        raise RuntimeError("补丁锚点 1（_refresh_page）对不上新版")
    out = out.replace(anchor1,
                      "            self._wait_game_page_loaded()\n"
                      "            self._install_cloud_ping_fallback()\n\n"
                      + method_text +
                      "\n    def _get_remaining_playtime", 1)
    anchor2 = ('                self.log_info("已在游戏中")\n                return\n'
               "            guide_close_btn")
    if out.count(anchor2) != 1:
        raise RuntimeError("补丁锚点 2（已在游戏中）对不上新版")
    out = out.replace(anchor2,
                      '                self.log_info("已在游戏中")\n                return\n'
                      "            self._install_cloud_ping_fallback()\n            guide_close_btn", 1)
    q_start = out.index("    def _wait_in_queue")
    q_end = out.index("            select_retries = 0", q_start)
    block = out[q_start:q_end]
    wait_pat = re.compile(r"(            status = WebDriverWait\(self\.driver, )\d+(\).until\()")
    if len(wait_pat.findall(block)) != 1:
        raise RuntimeError("补丁锚点 3（排队等待）对不上新版")
    block = wait_pat.sub(r"\1timeout\2", block, count=1)
    old_js = ('                    else if (document.querySelector(arguments[2])) return "select_queue";\n'
              "                    else return null;")
    if block.count(old_js) != 1:
        raise RuntimeError("补丁锚点 3（加载中判断）对不上新版")
    block = block.replace(old_js,
                          '                    else if (document.querySelector(arguments[2])) return "select_queue";\n'
                          '                    else if (document.body && document.body.innerText.includes("加载中")) return false;\n'
                          "                    else return null;", 1)
    out = out[:q_start] + block + out[q_end:]
    if out.count('includes("加载中")') != 1:
        raise RuntimeError("补丁校验失败：加载中判断出现次数异常")
    return out, "repatched"


def host_patch_path(spec):
    for m in spec.get("Mounts", []) or []:
        if m.get("Destination") == "/m7a/module/game/cloud.py":
            return m.get("Source")
    return PATCH_HOST_FALLBACK


def verify_patch_compiles(host_tmp_path, image):
    name = f"m7a-tmp-{int(time.time())}"
    body = {"Image": image, "Cmd": ["python", "-m", "py_compile", "/tmp/c.py"],
            "HostConfig": {"Binds": [f"{host_tmp_path}:/tmp/c.py:ro"], "AutoRemove": False}}
    code, data = docker_req("POST", f"/containers/create?name={name}", body=body)
    if code not in (200, 201):
        raise RuntimeError(f"创建校验容器失败: {code} {data}")
    try:
        code, _ = docker_req("POST", f"/containers/{name}/start", timeout=60)
        if code not in (200, 204, 304):
            raise RuntimeError(f"启动校验容器失败: {code}")
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            st = inspect(name)
            if not st or not st.get("State", {}).get("Running"):
                break
        st = inspect(name)
        ec = (st or {}).get("State", {}).get("ExitCode")
        if ec != 0:
            raise RuntimeError(f"补丁文件编译校验失败（exit {ec}）")
    finally:
        docker_req("DELETE", f"/containers/{name}?force=true")


def run_update():
    st = load_update()
    try:
        st.update(phase="pulling", message="正在拉取最新镜像…", started_at=int(time.time()), error=None)
        save_update(st)
        spec = inspect(CONTAINER)
        if not spec:
            raise RuntimeError("主容器不存在")
        image_ref = (spec.get("Config") or {}).get("Image", "")
        repo, tag = split_image_ref(image_ref)
        old_id = spec.get("Image")
        was_running = spec.get("State", {}).get("Running", False)
        code, raw = docker_req_bytes(
            "POST", f"/images/create?fromImage={urllib.parse.quote(repo, safe='')}&tag={urllib.parse.quote(tag, safe='')}",
            timeout=900)
        if code != 200:
            raise RuntimeError(f"拉取镜像失败: {code} {raw[:200]!r}")
        if b"errorDetail" in raw or b'"error":' in raw:
            raise RuntimeError(f"拉取镜像失败: {raw[-300:].decode('utf-8', 'replace')}")
        info = image_info(image_ref)
        new_id = (info or {}).get("Id")
        if not new_id:
            raise RuntimeError("拉取后读取镜像信息失败")
        if new_id == old_id:
            st.update(phase="done", message="已是最新版本，无需更新", finished_at=int(time.time()))
            save_update(st)
            return
        st.update(phase="patching", message="正在给新版重打排队补丁…")
        save_update(st)
        new_up = extract_from_image(new_id, "/m7a/module/game/cloud.py").decode("utf-8")
        panel_patch = os.path.join(M7A_DIR, "patches", "cloud.py")
        with open(panel_patch, encoding="utf-8") as f:
            current_patched = f.read()
        patched_text, mode = apply_cloud_patch(new_up, current_patched)
        host_patch = host_patch_path(spec)
        host_dir = os.path.dirname(host_patch)
        tmp_name = "cloud.py.updcheck"
        panel_tmp = os.path.join(M7A_DIR, "patches", tmp_name)
        with open(panel_tmp, "w", encoding="utf-8") as f:
            f.write(patched_text)
        try:
            st.update(phase="verifying", message="正在校验补丁文件…")
            save_update(st)
            verify_patch_compiles(os.path.join(host_dir, tmp_name), new_id)
            st.update(phase="installing", message="正在安装补丁并重建容器…")
            save_update(st)
            bak = os.path.join(M7A_DIR, "patches", f"cloud.py.bak-panel-{time.strftime('%Y%m%d')}")
            shutil.copyfile(panel_patch, bak)
            with open(panel_patch, "w", encoding="utf-8") as f:
                f.write(patched_text)
            with open(os.path.join(M7A_DIR, "patches", "cloud.py.upstream-" + time.strftime("%Y%m%d")), "w", encoding="utf-8") as f:
                f.write(new_up)
        finally:
            try:
                os.remove(panel_tmp)
            except OSError:
                pass
        st.update(phase="recreating", message="正在用新镜像重建容器…")
        save_update(st)
        body = clone_body(spec, strip_compose_labels=False)
        body["Image"] = new_id
        docker_req("POST", f"/containers/{CONTAINER}/stop?t=10", timeout=60)
        code, data = docker_req("DELETE", f"/containers/{CONTAINER}")
        if code not in (200, 204):
            raise RuntimeError(f"删除旧容器失败: {code} {data}")
        code, data = docker_req("POST", f"/containers/create?name={CONTAINER}", body=body)
        if code not in (200, 201):
            fb = clone_body(spec, strip_compose_labels=False)
            fb["Image"] = old_id
            docker_req("POST", f"/containers/create?name={CONTAINER}", body=fb)
            raise RuntimeError(f"按新镜像重建失败，已用旧镜像恢复: {code} {data}")
        if was_running:
            container_action("start")
        created = ((info or {}).get("Created") or "")[:10]
        st.update(phase="done", finished_at=int(time.time()),
                  message=f"已更新到 {created or new_id[:12]}（补丁：{'上游已内置' if mode == 'upstream_builtin' else '已重打'}）")
        save_update(st)
    except Exception as e:
        st.update(phase="failed", error=str(e), finished_at=int(time.time()))
        save_update(st)


def start_update():
    with _lock:
        if job_active():
            raise RuntimeError("已有任务在执行，暂不能更新")
        if update_active():
            raise RuntimeError("更新已在进行中")
        save_update({"phase": "pulling", "message": "正在准备…", "started_at": int(time.time()), "error": None})
    threading.Thread(target=run_update, daemon=True).start()


# ---------- 单项任务（一次性容器） ----------
def load_job():
    try:
        with open(JOB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"phase": "idle"}


def save_job(job):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = JOB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    os.replace(tmp, JOB_PATH)


def job_active(job=None):
    job = job or load_job()
    return job.get("phase") in ("stopping", "running", "restoring")


def _remove_oneshot():
    docker_req("POST", f"/containers/{ONESHOT}/stop?t=5", timeout=30)
    docker_req("DELETE", f"/containers/{ONESHOT}?force=true")


def _ensure_main_running():
    info = container_info()
    if info.get("exists") and not info.get("running"):
        container_action("start")


def run_job(task):
    job = load_job()
    try:
        job.update(phase="stopping")
        save_job(job)
        docker_req("POST", f"/containers/{CONTAINER}/stop?t=15", timeout=90)
        _remove_oneshot()
        spec = inspect(CONTAINER)
        if not spec:
            raise RuntimeError("主容器不存在")
        body = clone_body(spec, env_repl={"MARCH7TH_AFTER_FINISH": "Exit"},
                          cmd=["python", "main.py", task])
        body["HostConfig"]["RestartPolicy"] = {"Name": "no"}
        body["HostConfig"]["AutoRemove"] = False
        code, data = docker_req("POST", f"/containers/create?name={ONESHOT}", body=body)
        if code not in (200, 201):
            raise RuntimeError(f"创建任务容器失败: {code} {data}")
        container_action("start", ONESHOT)
        job.update(phase="running")
        save_job(job)
        deadline = time.time() + 4 * 3600
        exit_code = None
        while time.time() < deadline:
            time.sleep(5)
            st = inspect(ONESHOT)
            if not st:
                break
            if not st.get("State", {}).get("Running"):
                exit_code = st.get("State", {}).get("ExitCode")
                break
        else:
            _remove_oneshot()
            raise RuntimeError("任务超过 4 小时，已强制停止")
        if task == "character_export":
            try:
                blob = container_archive(ONESHOT, "/m7a/characters.json")
                if blob:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(os.path.join(DATA_DIR, "characters.json"), "wb") as f:
                        f.write(blob)
                    job["artifact"] = "characters.json"
            except Exception:
                pass
        job.update(phase="restoring")
        save_job(job)
        _remove_oneshot()
        _ensure_main_running()
        job.update(phase="done" if exit_code == 0 else "failed",
                   exit_code=exit_code, finished_at=int(time.time()))
        save_job(job)
    except Exception as e:
        try:
            _remove_oneshot()
            _ensure_main_running()
        except Exception:
            pass
        job.update(phase="failed", error=str(e), finished_at=int(time.time()))
        save_job(job)


def start_job(task):
    if task not in TASK_NAMES:
        raise ValueError("未知任务")
    with _lock:
        if job_active():
            raise RuntimeError("已有任务在执行")
        job = {"task": task, "name": TASK_NAMES[task], "phase": "stopping",
               "started_at": int(time.time()), "exit_code": None, "error": None}
        save_job(job)
    threading.Thread(target=run_job, args=(task,), daemon=True).start()
    return job


def reconcile_job():
    """面板启动时对账：任务进行中但一次性容器已不在 -> 恢复主容器并标记中断。"""
    job = load_job()
    if not job_active(job):
        return
    st = inspect(ONESHOT)
    if st and st.get("State", {}).get("Running"):
        threading.Thread(target=_reattach, args=(job.get("task"),), daemon=True).start()
    else:
        _remove_oneshot()
        try:
            _ensure_main_running()
        except Exception:
            pass
        job.update(phase="interrupted", finished_at=int(time.time()))
        save_job(job)


def _reattach(task):
    deadline = time.time() + 4 * 3600
    exit_code = None
    while time.time() < deadline:
        time.sleep(5)
        st = inspect(ONESHOT)
        if not st:
            break
        if not st.get("State", {}).get("Running"):
            exit_code = st.get("State", {}).get("ExitCode")
            break
    job = load_job()
    job.update(phase="restoring")
    save_job(job)
    _remove_oneshot()
    try:
        _ensure_main_running()
    except Exception:
        pass
    job.update(phase="done" if exit_code == 0 else "failed",
               exit_code=exit_code, finished_at=int(time.time()))
    save_job(job)


# ---------- 日志解析 ----------
def list_log_dates():
    try:
        files = [f[:-4] for f in os.listdir(LOG_DIR) if re.match(r"^\d{4}-\d{2}-\d{2}\.log$", f)]
    except FileNotFoundError:
        return []
    return sorted(files, reverse=True)


def read_log_lines(date=None, max_bytes=2_000_000):
    dates = list_log_dates()
    if not dates:
        return [], None
    date = date if date in dates else dates[0]
    path = os.path.join(LOG_DIR, date + ".log")
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        text = f.read().decode("utf-8", "replace")
    return text.splitlines(), date


LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| (\w+) \| (.*)$")
BALANCE_RE = re.compile(r"云游戏剩余时长：(\d+) 分钟（付费：(\d+) 分钟，免费：(\d+) 分钟）")


def parse_status():
    lines, date = read_log_lines()
    st = {"balance": None, "last_result": None, "last_run_start": None,
          "activity": None, "log_file": (date + ".log") if date else None}
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        ts, level, msg = m.groups()
        b = BALANCE_RE.search(msg)
        if b:
            st["balance"] = {"total": int(b.group(1)), "paid": int(b.group(2)), "free": int(b.group(3)), "time": ts}
        if "正在启动" in msg and "浏览器" in msg:
            st["last_run_start"] = ts
        if "进入云游戏成功" in msg:
            st["last_result"] = {"ok": True, "time": ts, "msg": "进入云游戏成功"}
        elif "进入云游戏失败" in msg or "尝试启动游戏时发生错误" in msg:
            st["last_result"] = {"ok": False, "time": ts, "msg": msg[:80]}
        if level == "INFO":
            st["activity"] = {"time": ts, "msg": msg[:120]}
    return st


def tail_logs(n=200, date=None):
    lines, used = read_log_lines(date)
    return "\n".join(lines[-n:]), used


# ---------- 配置读写（原地手术） ----------
def read_config_text():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return f.read()


def write_config_text(text):
    yaml.safe_load(text)
    with _lock:
        shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".panelbak")
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)


def set_scalar(text, key, value_repr):
    pat = re.compile(rf"^({re.escape(key)}:\s*)([^#\n]*?)(\s*)(#.*)?$", re.M)
    m = pat.search(text)
    if not m:
        raise KeyError(f"config 中找不到 {key}")
    comment = m.group(4) or ""
    return pat.sub(lambda _: f"{key}: {value_repr}" + (f"  {comment}" if comment else ""), text, count=1)


def get_scalar_text(text, key):
    m = re.search(rf"^{re.escape(key)}:\s*([^#\n]*?)\s*(?:#.*)?$", text, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def set_nested(text, section, subkey, value_repr):
    lines = text.splitlines(keepends=True)
    in_section = False
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}:\s*$", line):
            in_section = True
            continue
        if in_section:
            if line and not line[0].isspace():
                break
            m = re.match(rf"^(\s+{re.escape(subkey)}:\s*)([^#\n]*?)(\s*)(#.*)?\n?$", line)
            if m:
                comment = m.group(4) or ""
                nl = "\n" if line.endswith("\n") else ""
                lines[i] = f"  {subkey}: {value_repr}" + (f"  {comment}" if comment else "") + nl
                return "".join(lines)
    raise KeyError(f"config 中找不到 {section}.{subkey}")


def get_config_values():
    text = read_config_text()
    data = yaml.safe_load(text) or {}
    af = container_info().get("after_finish_env") or str(data.get("after_finish"))
    settings = {}
    for _g, key, _label, typ, _opts in FIELD_SPECS:
        raw = get_scalar_text(text, key)
        if typ == "bool":
            settings[key] = (raw or "").lower() in ("true", "1")
        elif typ == "int":
            try:
                settings[key] = int(float(raw)) if raw not in (None, "") else None
            except ValueError:
                settings[key] = None
        elif typ == "float":
            try:
                settings[key] = float(raw) if raw not in (None, "") else None
            except ValueError:
                settings[key] = None
        elif typ == "secret":
            settings[key] = "__SET__" if raw else ""
        else:
            settings[key] = raw if raw is not None else ""
    yaml_fields = {}
    for key, _label in YAML_FIELDS:
        val = data.get(key)
        yaml_fields[key] = "" if val is None else yaml.safe_dump(val, allow_unicode=True, sort_keys=False).strip()
    return {
        "after_finish": af,
        "instance_type": data.get("instance_type"),
        "instance_names": data.get("instance_names") or {},
        "challenge_counts": data.get("instance_names_challenge_count") or {},
        "power_plan_yaml": yaml.safe_dump(data.get("power_plan") or [], allow_unicode=True, sort_keys=False).strip(),
        "settings": settings,
        "yaml_fields": yaml_fields,
    }


def yaml_scalar(v):
    """把字符串写成 YAML 标量：需要时自动加引号，保证读回还是字符串。"""
    d = yaml.safe_dump(str(v), allow_unicode=True).strip()
    if d.endswith("\n..."):
        d = d[:-4].strip()
    return d


def apply_settings(text, fields):
    for key, val in (fields or {}).items():
        spec = SPEC_BY_KEY.get(key)
        if not spec:
            continue
        typ = spec[3]
        if typ == "bool":
            text = set_scalar(text, key, "true" if val else "false")
        elif typ == "int":
            text = set_scalar(text, key, str(int(val)))
        elif typ == "float":
            text = set_scalar(text, key, repr(float(val)))
        elif typ == "enum":
            if val not in (spec[4] or []):
                raise ValueError(f"{key} 取值非法")
            text = set_scalar(text, key, yaml_scalar(val))
        elif typ == "secret":
            if val in ("", "__SET__", None):
                continue  # 留空/未改动 -> 保持原值
            if val == "__CLEAR__":
                text = set_scalar(text, key, "''")
            else:
                text = set_scalar(text, key, yaml_scalar(str(val)))
        else:
            v = str(val).strip()
            if len(v) > 1000 or "\n" in v:
                raise ValueError(f"{key} 内容非法")
            text = set_scalar(text, key, yaml_scalar(v))
    return text


def replace_top_block(text, key, value):
    """整体替换一个顶层键的值（列表/字典/标量/空），保留文件其余部分。"""
    if value is None:
        block = f"{key}:\n"
    elif isinstance(value, list) and not value:
        block = f"{key}: []\n"
    elif isinstance(value, dict) and not value:
        block = f"{key}: {{}}\n"
    elif isinstance(value, (list, dict)):
        rendered = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).strip()
        block = f"{key}:\n" + "\n".join("  " + l for l in rendered.splitlines()) + "\n"
    elif isinstance(value, bool):
        block = f"{key}: {'true' if value else 'false'}\n"
    elif isinstance(value, (int, float)):
        block = f"{key}: {value}\n"
    else:
        block = f"{key}: {yaml_scalar(value)}\n"
    pat = re.compile(rf"^{re.escape(key)}:.*?(?=^[a-zA-Z_][\w]*:|\Z)", re.M | re.S)
    if not pat.search(text):
        raise KeyError(f"config 中找不到 {key}")
    return pat.sub(lambda _: block, text, count=1)


def replace_power_plan(text, plan_list):
    rendered = yaml.safe_dump(plan_list, allow_unicode=True, sort_keys=False).strip()
    block = "power_plan: " + ("[]" if not plan_list else "\n" + "\n".join("  " + l for l in rendered.splitlines())) + "\n"
    pat = re.compile(r"^power_plan:.*?(?=^[a-zA-Z_][\w]*:|\Z)", re.M | re.S)
    if not pat.search(text):
        raise KeyError("config 中找不到 power_plan")
    return pat.sub(block, text, count=1)


def update_compose_after_finish(mode):
    try:
        with open(COMPOSE_PATH, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return False
    if re.search(r"MARCH7TH_AFTER_FINISH[=:\s]", text):
        text = re.sub(r"(MARCH7TH_AFTER_FINISH[=:]\s*)\S+", rf"\g<1>{mode}", text, count=1)
    elif re.search(r"^\s+environment:\s*$", text, re.M):
        text = re.sub(r"^(\s+environment:\s*)$", rf"\1\n      - MARCH7TH_AFTER_FINISH={mode}", text, count=1, flags=re.M)
    else:
        return False
    with _lock:
        with open(COMPOSE_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    return True

def qrcode_path():
    p = os.path.join(LOG_DIR, "qrcode_login.png")
    return p if os.path.isfile(p) else None


def list_screenshots():
    d = os.path.join(LOG_DIR, "screenshots")
    try:
        out = []
        for f in sorted(os.listdir(d), reverse=True):
            p = os.path.join(d, f)
            if os.path.isfile(p) and f.lower().endswith((".png", ".jpg", ".jpeg")):
                st = os.stat(p)
                out.append({"name": f, "size": st.st_size, "mtime": int(st.st_mtime)})
        return out
    except FileNotFoundError:
        return []


# ---------- HTTP ----------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0d0f15">
<title>三月七助手 · 面板</title>
<style>
:root{color-scheme:dark;--bg:#0d0f15;--cd:#161922;--cd2:#1d212c;--in:#0a0c11;--ln:#2a2f3c;--tx:#e8eaf0;--mu:#8e94a6;--lb:#c4c9d4;--pr:#3b7bff;--ok:#3fb950;--bad:#f85149;--wn:#d29922;--run:#58a6ff;--r:16px;--nav:60px;--sb:env(safe-area-inset-bottom,0px)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{margin:0;background:radial-gradient(900px 360px at 50% -160px,#1b2442,transparent) no-repeat,var(--bg);color:var(--tx);font:14px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding-bottom:calc(var(--nav) + var(--sb) + 16px)}
a{color:#9fb4e8}
.wrap{max-width:1080px;margin:0 auto;padding:0 12px}
header{max-width:1080px;margin:0 auto;padding:18px 14px 10px;display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,#ff8fc8,#7aa8ff);display:grid;place-items:center;font-size:22px;flex:none;box-shadow:0 4px 14px rgba(122,168,255,.3)}
h1{font-size:20px;margin:0;line-height:1.2}
.sub{color:var(--mu);font-size:13px}
section{scroll-margin-top:12px}
.card{background:var(--cd);border:1px solid var(--ln);border-radius:var(--r);padding:16px;margin-bottom:12px;box-shadow:0 6px 20px rgba(0,0,0,.25)}
h2{font-size:17px;margin:0;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:4px;height:16px;border-radius:2px;background:var(--pr)}
h3{font-size:15px;margin:20px 0 4px;color:var(--lb)}
.ch{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:4px;flex-wrap:wrap}
.k{color:var(--mu);font-size:13px}
.small{font-size:13px;color:var(--mu)}
.desc{margin:4px 0 12px}
.msg{font-size:13px;min-height:20px;margin-top:8px;color:var(--lb)}
button{-webkit-appearance:none;appearance:none;background:var(--pr);color:#fff;border:0;border-radius:12px;padding:10px 16px;min-height:44px;font-size:15px;font-weight:500;cursor:pointer;margin:0;transition:transform .08s,filter .15s,background .15s;font-family:inherit}
button:active:not(:disabled){transform:scale(.97);filter:brightness(.88)}
button.warn{background:#a66d05}button.okb{background:#238636}
button.ghost{background:var(--cd2);border:1px solid var(--ln);color:var(--tx)}
button:disabled{opacity:.42;cursor:not-allowed}
input:not([type=checkbox]),select,textarea{-webkit-appearance:none;appearance:none;width:100%;background:var(--in);color:var(--tx);border:1px solid var(--ln);border-radius:10px;padding:10px 12px;min-height:44px;font-size:16px;font-family:inherit;outline:none;transition:border-color .15s}
input:focus,select:focus,textarea:focus{border-color:var(--pr);box-shadow:0 0 0 3px rgba(59,123,255,.18)}
select{padding-right:34px;background-image:linear-gradient(45deg,transparent 50%,var(--mu) 50%),linear-gradient(135deg,var(--mu) 50%,transparent 50%);background-position:calc(100% - 18px) 52%,calc(100% - 13px) 52%;background-size:5px 5px;background-repeat:no-repeat}
textarea{font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;min-height:130px;resize:vertical}
input[type=checkbox]{width:20px;height:20px;accent-color:var(--pr);margin:0}
.lb{display:block;font-size:13px;color:var(--lb);margin:12px 0 6px}
.sw{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:48px;cursor:pointer;position:relative;font-size:14px;color:var(--tx)}
.sw input{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}
.sw i{flex:none;width:48px;height:28px;border-radius:14px;background:#3a3f4e;position:relative;transition:background .2s}
.sw i::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.4)}
.sw input:checked+i{background:var(--pr)}.sw input:checked+i::after{transform:translateX(20px)}
.sw input:focus-visible+i{outline:2px solid var(--run);outline-offset:2px}
.sw.mini{justify-content:flex-start;color:var(--lb);font-size:13px}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;flex:none}
.ok{background:var(--ok);box-shadow:0 0 0 4px rgba(63,185,80,.18)}.bad{background:var(--bad);box-shadow:0 0 0 4px rgba(248,81,73,.18)}.idle{background:var(--mu)}
.hero{border-top:3px solid var(--mu)}.hero[data-st=ok]{border-top-color:var(--ok)}.hero[data-st=bad]{border-top-color:var(--bad)}.hero[data-st=run]{border-top-color:var(--run)}
.state{display:flex;align-items:center;gap:10px;font-size:21px;font-weight:600;margin:4px 0 2px}
.ctl{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}.ctl .big{grid-column:1/-1;min-height:52px;font-size:16px}
.jobbar{display:none;margin-top:12px;padding:11px 14px;border-radius:12px;font-size:14px;background:rgba(88,166,255,.12);border:1px solid rgba(88,166,255,.5);color:#cfe3ff}
.jobbar.done{background:rgba(63,185,80,.1);border-color:rgba(63,185,80,.5);color:#bfe9c6}.jobbar.failed{background:rgba(248,81,73,.1);border-color:rgba(248,81,73,.5);color:#ffcdc9}.jobbar.interrupted{background:rgba(210,153,34,.1);border-color:rgba(210,153,34,.5);color:#f2dca6}
.duo,.side{display:grid;gap:12px;align-content:start}.duo{margin-bottom:12px}.duo .card{margin:0}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}.stats .card{margin:0}.stats .act{grid-column:1/-1}
.tile .v{font-size:18px;font-weight:600;margin:4px 0 2px}.tile[data-st=ok] .v{color:var(--ok)}.tile[data-st=bad] .v{color:var(--bad)}
.bal{display:flex;gap:16px;margin:4px 0 2px}.bal b{font-size:24px;display:block;line-height:1.2}.bal span{font-size:13px;color:var(--mu)}
#activity{font-size:15px;margin:4px 0 2px;word-break:break-all}
.qrw{margin-top:12px;text-align:center}#qrimg{width:220px;height:220px;background:#fff;border-radius:14px;padding:8px}
.tg{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.tg button{background:var(--cd2);border:1px solid var(--ln);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:10px 6px;min-height:66px;font-size:14px;color:var(--tx)}
.tg button b{font-size:20px;font-weight:400;line-height:1.2}
.tb{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}.tb select{flex:1 1 150px;width:auto}.tb input#logfilter{flex:1 1 160px;width:auto}
pre{background:var(--in);border:1px solid var(--ln);border-radius:12px;padding:12px;font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;min-height:200px;max-height:60vh;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:10px 0;-webkit-overflow-scrolling:touch}
.btns{display:flex;gap:10px;flex-wrap:wrap}.btns button{flex:1}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin:12px 0}
.shots a{display:block;border-radius:10px;overflow:hidden;background:#000;aspect-ratio:16/9;border:1px solid var(--ln)}
.shots img{width:100%;height:100%;object-fit:cover;display:block}
.ftool{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 6px}.ftool input{flex:1 1 100%}.ftool button{flex:1}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 8px}.chips button{min-height:38px;padding:6px 13px;font-size:13px;border-radius:19px;font-weight:400}
.grp{border:1px solid var(--ln);border-radius:12px;margin-top:8px;background:var(--cd2);overflow:hidden;scroll-margin-top:12px}
.grp-h{width:100%;display:flex;align-items:center;gap:8px;background:none;padding:12px 14px;min-height:52px;color:var(--tx);font-size:15px;text-align:left;border-radius:0}
.grp-h span{flex:1}.grp-h em{font-style:normal;font-size:13px;color:var(--mu);background:var(--bg);padding:1px 9px;border-radius:10px}
.grp-h i{font-style:normal;color:var(--mu);transition:transform .2s;font-size:18px}.grp-h i::before{content:"›"}
.grp.open .grp-h i,.grp.hit .grp-h i{transform:rotate(90deg)}
.grp-b{display:none;padding:2px 14px 14px;border-top:1px solid var(--ln);column-gap:20px}
.grp.open .grp-b,.grp.hit .grp-b{display:grid;grid-template-columns:1fr}
.hide{display:none!important}
.sec{display:flex;gap:10px;align-items:center}.clr{display:flex;align-items:center;gap:6px;min-height:44px;white-space:nowrap;font-size:13px;color:var(--mu);cursor:pointer}
.pair{background:var(--cd2);border:1px solid var(--ln);border-radius:12px;padding:4px 12px 12px;margin-top:8px}
.pair .t{font-size:14px;margin-top:8px;color:var(--tx)}.pair .r{display:grid;grid-template-columns:1fr 104px;gap:10px}.pair .lb{margin-top:6px}
.savebar{position:sticky;bottom:calc(var(--nav) + var(--sb) + 8px);z-index:5;display:flex;align-items:center;gap:12px;background:rgba(29,33,44,.97);border:1px solid var(--ln);border-radius:14px;padding:8px 12px 8px 8px;margin-top:16px;box-shadow:0 -6px 24px rgba(0,0,0,.45)}
.savebar button{flex:none;min-height:48px}.savebar .msg{margin-top:2px;min-height:0}
.yrow{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}.yrow .msg{margin:0}
#yamlBox{display:grid;gap:12px;margin-top:8px}#yamlBox>.warnt{grid-column:1/-1;margin:0}
.ed{background:var(--cd2);border:1px solid var(--ln);border-radius:12px;padding:12px;min-width:0}
.ed-h{font-size:14.5px;font-weight:600;margin-bottom:2px}.ed-h .k{font-weight:400;font-size:12.5px;word-break:break-all}
.ed textarea{min-height:90px;margin-top:8px}
.er{display:grid;gap:8px;align-items:center;margin-top:8px}.er>*{min-width:0}
.er.t2{grid-template-columns:1fr 138px}.er.c2{grid-template-columns:1fr 1fr 44px}.er.c1{grid-template-columns:1fr 44px}.er.cn{grid-template-columns:1fr 92px 44px}.er.cs{grid-template-columns:1fr auto 44px}.er.kv{grid-template-columns:1fr 1.3fr 44px}.er.st{grid-template-columns:1fr auto 92px}.er.rg{grid-template-columns:1fr auto 1fr}
.xb{width:44px;padding:0;font-size:15px;color:var(--mu)!important}.addb{margin-top:8px;width:100%;font-size:14px}
.warnt{color:var(--wn);margin:6px 0 0}
.cp{position:relative}
.cpl{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:30;min-width:180px;max-height:352px;overflow:auto;background:var(--cd);border:1px solid var(--ln);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.55);-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.cpo{display:flex;justify-content:space-between;align-items:center;gap:8px;min-height:44px;padding:0 12px;cursor:pointer;border-bottom:1px solid var(--ln)}
.cpo:last-child{border-bottom:0}.cpo:active{background:var(--cd2)}.cpo em{font-style:normal;font-size:12px;color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;background:rgba(18,21,29,.94);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-top:1px solid var(--ln);padding-bottom:var(--sb)}
.nav a{flex:1;min-height:var(--nav);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;color:var(--mu);text-decoration:none;font-size:13px}
.nav a b{font-size:18px;font-weight:400;line-height:1.2}.nav a.on{color:var(--run)}
@media(hover:hover){.cpo:hover{background:var(--cd2)}}
@media(min-width:560px){.tg{grid-template-columns:repeat(3,1fr)}.stats{grid-template-columns:1fr 1fr 1.4fr}.stats .act{grid-column:auto}.ftool input{flex:1 1 240px}.ftool button{flex:0 0 auto}}
@media(min-width:900px){body{padding-bottom:24px}section,.grp{scroll-margin-top:64px}.nav{position:sticky;top:0;bottom:auto;justify-content:center;border-top:0;border-bottom:1px solid var(--ln);padding:0}.nav a{flex:0 0 112px;flex-direction:row;gap:6px;min-height:50px;font-size:14px}.nav a b{font-size:16px}.duo{grid-template-columns:1.4fr 1fr}.tg{grid-template-columns:repeat(4,1fr)}.grp.open .grp-b,.grp.hit .grp-b{grid-template-columns:1fr 1fr}.savebar{bottom:12px}.wrap{padding:12px 16px 0}#yamlBox{grid-template-columns:1fr 1fr}}
</style></head><body>
<header><div class="logo">🌸</div><div><h1>三月七助手</h1><div class="sub">云·星穹铁道自动日常 · 悉尼服务器</div></div></header>
<nav class="nav"><a href="#s-status"><b>◉</b>状态</a><a href="#s-task"><b>⚡</b>任务</a><a href="#s-log"><b>☰</b>日志</a><a href="#s-shot"><b>▣</b>截图</a><a href="#s-cfg"><b>⚙</b>设置</a></nav>
<div class="wrap">
<section id="s-status">
<div class="duo">
  <div class="card hero" id="hero">
    <div class="k">容器状态</div>
    <div class="state"><span id="dot" class="dot idle"></span><span id="cstate">读取中…</span></div>
    <div class="small" id="cmeta"></div>
    <div class="jobbar" id="jobbar"></div>
    <div class="ctl"><button id="btnRestart" class="big">▶ 立即重启（跑一轮）</button><button id="btnStop" class="warn">■ 停止</button><button id="btnStart" class="okb">⏵ 启动</button></div>
    <div class="msg" id="cmsg"></div>
  </div>
  <div class="side">
    <div class="card" id="qrcard" style="display:none">
      <h2>云游戏扫码登录</h2>
      <div class="small desc">用米游社 App 扫下面二维码完成登录。<span id="qrtime"></span></div>
      <div class="qrw"><img id="qrimg" alt="登录二维码"></div>
    </div>
    <div class="card">
      <div class="k">小助手版本</div>
      <div style="font-size:14.5px;margin:4px 0 10px;word-break:break-all" id="verinfo">读取中…</div>
      <button id="btnUpdate" class="ghost" style="width:100%">⟳ 更新小助手</button>
      <div class="small" style="margin-top:6px">拉取最新镜像并自动重打排队补丁。</div>
      <div class="msg" id="updmsg"></div>
    </div>
  </div>
</div>
<div class="stats">
  <div class="card tile"><div class="k">云游戏剩余时长</div><div class="bal"><div><b id="balFree">—</b><span>免费 · 分钟</span></div><div><b id="balPaid">—</b><span>付费 · 分钟</span></div></div><div class="small" id="balanceTime"></div></div>
  <div class="card tile" id="resTile"><div class="k">最近一次进游戏</div><div class="v" id="result">—</div><div class="small" id="resultTime"></div></div>
  <div class="card tile act"><div class="k">当前动态</div><div id="activity">—</div><div class="small" id="activityTime"></div></div>
</div>
</section>

<section id="s-task"><div class="card">
  <h2>快捷任务（单独执行）</h2>
  <div class="small desc">执行时会先停下常驻容器、用一次性容器跑这个任务、跑完自动恢复常驻。白天可能要排队，进度看下方日志。</div>
  <div class="tg" id="taskgrid"></div>
  <div class="msg" id="taskmsg"></div>
  <div class="msg" id="artifact"></div>
</div></section>

<section id="s-log"><div class="card">
  <h2>运行日志</h2>
  <div class="tb">
    <select id="logdate"></select>
    <input id="logfilter" type="search" placeholder="过滤关键字">
    <label class="sw mini"><input type="checkbox" id="autolog" checked><i></i><span>自动刷新</span></label>
  </div>
  <pre id="logs">加载中…</pre>
  <div class="btns"><button id="btnLogs" class="ghost">⟳ 刷新日志</button><button id="btnBottom" class="ghost">↓ 回到底部</button></div>
</div></section>

<section id="s-shot"><div class="card">
  <h2>游戏截图</h2>
  <div class="small desc">程序运行时截取的游戏画面（用于排查异常），点开放大。</div>
  <div class="shots" id="shots"></div>
  <button id="btnShots" class="ghost" style="width:100%">⟳ 刷新截图</button>
</div></section>

<section id="s-cfg">
<div class="card">
  <div class="ch"><h2>运行设置</h2><button id="btnSaveCfg2">保存全部</button></div>
  <div class="msg" id="cfgmsg2" style="margin-top:2px"></div>
  <label class="lb" for="afterFinish">循环模式（改后会重建容器，约十几秒）</label>
  <select id="afterFinish">
    <option value="Loop">Loop — 跑完等待，到定时时间再跑</option>
    <option value="None">None — 跑完立即重跑（会烧时长，不推荐）</option>
    <option value="Exit">Exit — 跑完退出程序</option>
  </select>
  <div class="ftool"><input id="cfgfilter" type="search" placeholder="🔍 搜索分组或设置项"><button id="btnOpenAll" class="ghost">全部展开</button><button id="btnCloseAll" class="ghost">全部折叠</button></div>
  <div class="chips" id="grpchips"></div>
  <div class="small" id="cfgcount"></div>
  <div id="settingsBox"><div class="small">加载中…</div></div>
  <h3>副本设置</h3>
  <label class="lb" for="instType">默认副本类型</label>
  <select id="instType"></select>
  <div id="counts"></div>
  <div class="savebar"><button id="btnSaveCfg">保存全部设置</button><div><div class="small">普通设置下一轮生效；循环模式立即重建容器。</div><div class="msg" id="cfgmsg"></div></div></div>
</div>
<div class="card">
  <h2>体力计划 power_plan（YAML）</h2>
  <div class="small desc">留空数组 [] 表示不设专门计划，用上面的默认副本设置。保存时会校验格式。</div>
  <textarea id="planYaml" spellcheck="false"></textarea>
  <div class="yrow"><button id="btnSavePlan">保存体力计划</button><span class="msg" id="planmsg"></span></div>
</div>
<div class="card">
  <h2>名单与结构化配置</h2>
  <div class="small desc">队伍、支援名单、站点优先级、关卡范围等结构化内容直接用表单编辑，和桌面面板是同一份配置。每块独立保存，格式不对会报错、不落盘。</div>
  <div id="yamlBox"><div class="small">加载中…</div></div>
</div>
</section>
</div>
<script>
const $=id=>document.getElementById(id);
const mk=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
async function api(path,opt){const r=await fetch(path,opt);const t=await r.text();let j;try{j=JSON.parse(t)}catch(e){j={error:t}}if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
function fmtT(s){return s?s.replace('T',' ').slice(0,19):''}
let jobPhase='idle',jobActive=false;
function renderJob(job){
  jobPhase=job.phase||'idle';
  const bar=$('jobbar');
  const active=jobActive=['stopping','running','restoring'].includes(jobPhase);
  const names={stopping:'正在准备（停常驻容器）…',running:'正在执行：'+(job.name||job.task),restoring:'任务结束，正在恢复常驻容器…',done:'上次任务「'+(job.name||'')+'」已完成',failed:'上次任务「'+(job.name||'')+'」失败'+(job.error?('：'+job.error):''),interrupted:'上次任务被中断（常驻容器已恢复）'};
  if(jobPhase!=='idle'&&names[jobPhase]){bar.style.display='block';bar.className='jobbar '+(active?'':jobPhase);bar.textContent=(active?'⏳ ':'')+names[jobPhase]}
  else bar.style.display='none';
  $('artifact').innerHTML=job.artifact?'角色面板已导出：<a href="/api/characters">⬇ 下载 characters.json</a>':'';
  document.querySelectorAll('#taskgrid button').forEach(b=>b.disabled=active);
  ['btnRestart','btnStop','btnStart'].forEach(i=>$(i).disabled=active);
}
async function loadStatus(){
  try{
    const s=await api('/api/status');
    renderJob(s.job||{});
    const c=s.container,st=c.running?'ok':(c.exists?'bad':'idle');
    $('dot').className='dot '+st;$('hero').dataset.st=jobActive?'run':st;
    $('cstate').textContent=c.exists?(c.running?'运行中':'已停止 · '+c.status):'容器不存在';
    $('cmeta').textContent=c.exists?('镜像 '+c.image+' · 启动于 '+fmtT(c.started_at)):'';
    const b=s.log.balance;
    $('balFree').textContent=b?b.free:'—';$('balPaid').textContent=b?b.paid:'—';
    $('balanceTime').textContent=b?('记录时间 '+b.time):'';
    const r=s.log.last_result;
    $('result').textContent=r?(r.ok?'✅ 成功':'❌ 失败'):'—';
    $('resTile').dataset.st=r?(r.ok?'ok':'bad'):'';
    $('resultTime').textContent=r?(r.time+' · '+r.msg):'';
    const a=s.log.activity;
    $('activity').textContent=a?a.msg:'—';
    $('activityTime').textContent=a?a.time:'';
  }catch(e){$('cstate').textContent='读取失败：'+e.message}
}
async function loadDates(){
  const d=await api('/api/logdates');
  const sel=$('logdate'),cur=sel.value;
  sel.innerHTML='';
  (d.dates||[]).forEach(dt=>{const o=mk('option','',dt+(dt===d.dates[0]?'（最新）':''));o.value=dt;sel.appendChild(o)});
  if(cur&&d.dates.includes(cur))sel.value=cur;
}
async function loadLogs(){
  try{const j=await api('/api/logs?lines=220&date='+encodeURIComponent($('logdate').value||''));
    let text=j.text||'(无日志)';
    const q=$('logfilter').value.trim();
    if(q){const lines=text.split('\n').filter(l=>l.includes(q));text=lines.length?lines.join('\n'):'(无匹配行)'}
    const p=$('logs'),nearBottom=p.scrollHeight-p.scrollTop-p.clientHeight<80;
    p.textContent=text;if(nearBottom)p.scrollTop=p.scrollHeight;
  }catch(e){$('logs').textContent='读取失败：'+e.message}
}
let updTimer=null;
async function loadVersion(){
  try{const v=await api('/api/version');
    $('verinfo').textContent=v.image?('镜像 '+v.image+' · 构建于 '+fmtT(v.created||'')):'读取失败';
    renderUpdate(v.update||{});
  }catch(e){$('verinfo').textContent='读取失败：'+e.message}
}
function renderUpdate(u){
  const m=$('updmsg');
  const active=['pulling','patching','verifying','installing','recreating'].includes(u.phase);
  m.textContent=(u.phase&&u.phase!=='idle')?((active?'⏳ ':'')+(u.message||u.phase)+(u.error?('：'+u.error):'')):'';
  $('btnUpdate').disabled=active;
  if(active&&!updTimer)updTimer=setInterval(loadVersion,3000);
  if(!active&&updTimer){clearInterval(updTimer);updTimer=null}
}
async function loadQR(){
  try{const r=await fetch('/api/qrcode');
    if(r.ok){$('qrcard').style.display='block';$('qrimg').src='/api/qrcode?t='+Date.now()}
    else $('qrcard').style.display='none';
  }catch(e){$('qrcard').style.display='none'}
}
async function loadShots(){
  try{const j=await api('/api/screenshots');
    const box=$('shots');box.innerHTML='';
    if(!j.shots.length){box.innerHTML='<span class="small">暂无截图</span>';return}
    j.shots.slice(0,24).forEach(s=>{
      const a=mk('a');a.href='/api/screenshot?name='+encodeURIComponent(s.name);a.target='_blank';
      const im=mk('img');im.src=a.href;im.loading='lazy';im.alt=s.name;
      im.title=s.name+' · '+new Date(s.mtime*1000).toLocaleString();
      a.appendChild(im);box.appendChild(a);
    });
  }catch(e){$('shots').innerHTML='<span class="small">读取失败</span>'}
}
/* ===== 结构化编辑器：工具 ===== */
const isA=Array.isArray,isO=x=>!!x&&typeof x==='object'&&!isA(x),isS=x=>x==null||typeof x!=='object',S=x=>x==null?'':String(x);
const FB='该字段结构较复杂，已回退为 YAML 文本编辑';
const DT=[[-1,"秘技 / 开怪"],[0,"无操作"],[1,"秘技 1 次"],[2,"秘技 2 次"]],DS={"战斗":1,"精英":1,"事件":2,"奖励":2,"异常":2,"铸造":2},DF=["首领","休整","转化"];
let RO=null,RP=null;const getRoster=()=>RO;
const loadRoster=()=>RP||(RP=api('/api/roster').then(r=>{if(!isA(r.characters))throw 0;RO={characters:r.characters,techniques:isA(r.techniques)&&r.techniques.length?r.techniques:DT,stations:isO(r.stations)?r.stations:DS,fixed_stations:isA(r.fixed_stations)?r.fixed_stations:DF}}).catch(()=>{RO={fail:1,characters:null,techniques:DT,stations:DS,fixed_stations:DF}}).then(()=>RO));
function parseMiniYaml(src){
  const L=String(src??'').split(/\r?\n/).map(l=>l.replace(/\s+$/,'')).filter(l=>l.trim()&&!/^\s*#/.test(l)&&!/^(---|\.\.\.)$/.test(l)).map(l=>[l.length-l.trimStart().length,l.trim()]);
  if(!L.length)return null;
  let i=0;const KEY=/^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s"'#\[\]{}|>&*!%@`,-][^:]*?):(?:\s+(.*))?$/,dash=t=>t==='-'||t.startsWith('- ');
  const sc=t=>{t=t.trim();
    if(/^(~|null|Null|NULL)?$/.test(t))return null;
    if(/^(true|True|TRUE)$/.test(t))return true;if(/^(false|False|FALSE)$/.test(t))return false;
    if(t[0]==='"')return JSON.parse(t);
    if(t[0]==="'"){if(!/^'(?:[^']|'')*'$/.test(t))throw 0;return t.slice(1,-1).replace(/''/g,"'")}
    if(t[0]==='['){if(t.slice(-1)!==']')throw 0;const b=t.slice(1,-1),o=[];let c='',q='';if(!b.trim())return o;
      for(const h of b){if(q){c+=h;if(h===q)q=''}else if(h==='"'||h==="'"){q=h;c+=h}else if('[]{}'.includes(h))throw 0;else if(h===','){o.push(sc(c));c=''}else c+=h}
      o.push(sc(c));return o}
    if(t==='{}')return{};
    if(/^[{&*!|>%@`]/.test(t)||/\s#/.test(t))throw 0;
    if(/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t))return Number(t);
    return t};
  const blk=ind=>{const t=L[i][1];
    if(dash(t)){const a=[];
      while(i<L.length&&L[i][0]===ind&&dash(L[i][1])){const s=L[i][1],r=s.slice(1).trimStart();
        if(!r){i++;a.push(i<L.length&&L[i][0]>ind?blk(L[i][0]):null)}
        else{L[i]=[ind+s.length-r.length,r];a.push(blk(L[i][0]))}}
      return a}
    if(KEY.test(t)){const o={};
      while(i<L.length&&L[i][0]===ind){const m=L[i][1].match(KEY);if(!m)throw 0;const k=/^["']/.test(m[1])?String(sc(m[1])):m[1].trim();i++;
        o[k]=m[2]!=null&&m[2].trim()!==''?sc(m[2]):(i<L.length&&(L[i][0]>ind||L[i][0]===ind&&dash(L[i][1]))?blk(L[i][0]):null)}
      return o}
    i++;return sc(t)};
  const v=blk(L[0][0]);if(i<L.length)throw 0;return v;
}
const pv=x=>x==null||typeof x==='string'?parseMiniYaml(x):x,rawT=x=>x==null?'':typeof x==='string'?x:JSON.stringify(x,null,2);
const Er=(t,full)=>Object.assign(new Error(t),{full});
const saveJ=(key,value)=>post('/api/yamlfield_json',{key,value}).then(r=>r.msg||'已保存');
const tIn=(v,ph,type)=>{const e=mk('input');if(type){e.type=type;e.inputMode='numeric'}if(ph)e.placeholder=ph;e.value=S(v);return e};
function swEl(on){const l=mk('label','sw'),cb=mk('input');cb.type='checkbox';cb.checked=!!on;l.append(cb,mk('i'));return[l,cb]}
function edBox(t,key){const w=mk('div','ed'),h=mk('div','ed-h',t);if(key)h.append(mk('span','k',' · '+key));w.append(h);return w}
function saveRow(w,fn){const row=mk('div','yrow'),b=mk('button','','保存'),m=mk('span','msg');b.type='button';
  b.onclick=async()=>{m.textContent='保存中…';b.disabled=true;try{m.textContent=await fn()}catch(e){m.textContent=(e.full?'':'失败：')+e.message}b.disabled=false};
  row.append(b,m);w.append(row)}
function edRaw(box,key,label,txt,note){const w=edBox(label,key);if(note)w.append(mk('div','small warnt',note));const ta=mk('textarea');ta.spellcheck=false;ta.value=txt;w.append(ta);
  saveRow(w,()=>post('/api/yamlfield',{key,yaml:ta.value}).then(r=>r.msg||'已保存'));box.append(w)}
function makeCharPicker(initId,getRoster){
  const R=getRoster(),C=R&&R.characters,inp=mk('input');inp.autocomplete='off';inp.spellcheck=false;inp.setAttribute('autocapitalize','off');
  if(!C){inp.placeholder='角色英文 ID';inp.value=initId||'';return{el:inp,get:()=>inp.value.trim()}}
  const w=mk('div','cp'),ul=mk('div','cpl hide'),nm=x=>(C.find(c=>c[0]===x)||[0,x])[1];let id=initId||'';
  inp.value=id?nm(id):'';inp.placeholder='搜索角色';
  const close=()=>ul.classList.add('hide');
  const draw=all=>{const q=all?'':inp.value.trim().toLowerCase(),hs=C.filter(c=>!q||String(c[1]).toLowerCase().includes(q)||String(c[0]).toLowerCase().includes(q));ul.innerHTML='';
    hs.forEach(c=>{const o=mk('div','cpo');o.append(mk('span','',c[1]),mk('em','',c[0]));o.onclick=()=>{id=c[0];inp.value=c[1];close();inp.blur()};ul.append(o)});
    ul.classList.toggle('hide',!hs.length);ul.scrollTop=0};
  ul.onmousedown=e=>e.preventDefault();
  inp.oninput=()=>{id='';draw()};inp.onfocus=()=>{inp.select();draw(true)};inp.onblur=()=>setTimeout(close,200);
  inp.onkeydown=e=>{if(e.key==='Escape')close();else if(e.key==='Enter'&&!ul.classList.contains('hide')&&ul.firstChild){e.preventDefault();ul.firstChild.click()}};
  w.append(inp,ul);
  return{el:w,get:()=>{const v=inp.value.trim();if(!v)return'';if(id&&v===nm(id))return id;const f=C.find(c=>c[1]===v||String(c[0]).toLowerCase()===v.toLowerCase());return f?f[0]:v}};
}
function techSel(v){const s=mk('select');RO.techniques.forEach(([n,t])=>{const o=mk('option','',t);o.value=String(n);s.append(o)});
  v=v==null?'0':String(v);if(![...s.options].some(o=>o.value===v)){const o=mk('option','',v);o.value=v;s.append(o)}s.value=v;return s}
function rowsEd(w,init,cls,mkRow){const box=mk('div'),G=[];
  const add=it=>{const r=mk('div','er '+cls),g=mkRow(r,it),x=mk('button','ghost xb','✕');x.type='button';x.setAttribute('aria-label','删除此行');
    x.onclick=()=>{r.remove();G.splice(G.indexOf(g),1)};r.append(x);G.push(g);box.append(r)};
  (init&&init.length?init:[undefined]).forEach(add);
  const ab=mk('button','ghost addb','+ 添加一行');ab.type='button';ab.onclick=()=>add();w.append(box,ab);return()=>G.map(g=>g())}
const dictSave=(key,g)=>()=>{const o={};g().forEach(([k,x])=>{if(k)o[k]=x});return saveJ(key,o)};
function edTeam(w,key,v){const G=[];
  for(let k=0;k<4;k++){const it=(v||[])[k]||[],r=mk('div','er t2'),p=makeCharPicker(S(it[0]),getRoster),s=techSel(it[1]);r.append(p.el,s);w.append(r);G.push(()=>[p.get(),+s.value])}
  saveRow(w,()=>saveJ(key,G.map(g=>g()).filter(x=>x[0])))}
function edBorrow(w,key,v){const g=rowsEd(w,v,'c2',(r,it)=>{it=it||[];const p=makeCharPicker(S(it[0]),getRoster),n=tIn(it[1],'好友昵称');r.append(p.el,n);return()=>[p.get(),n.value.trim()]});
  saveRow(w,()=>saveJ(key,g().filter(x=>x[0])))}
function edRange(w,key,v){const a=tIn(v&&v.length?v[0]:'','起始','number'),b=tIn(v&&v.length?v[1]:'','结束','number'),r=mk('div','er rg');r.append(a,mk('span','k','—'),b);w.append(r);
  saveRow(w,()=>{const x=a.value.trim(),y=b.value.trim();if(!/^-?\d+$/.test(x)||!/^-?\d+$/.test(y)||+x>+y)throw Er('起始和结束都必须填整数，且起始不能大于结束');return saveJ(key,[+x,+y])})}
function edInst(w,key,v){const g=rowsEd(w,v,'cn',(r,it)=>{it=it||{};const a=tIn(it.instance_name,'副本名'),b=tIn(it.team_number,'队伍','number');r.append(a,b);return()=>({instance_name:a.value.trim(),team_number:parseInt(b.value,10)||0})});
  saveRow(w,()=>saveJ(key,g().filter(x=>x.instance_name)))}
function edDaily(w,key,v){const g=rowsEd(w,v&&Object.entries(v),'cs',(r,it)=>{it=it||['',true];const a=tIn(it[0],'任务名'),[l,cb]=swEl(it[1]);r.append(a,l);return()=>[a.value.trim(),cb.checked]});
  saveRow(w,dictSave(key,g))}
function edStr(w,key,v){const g=rowsEd(w,v,'c1',(r,it)=>{const a=tIn(it,key==='redemption_code'?'兑换码':'每行一项');r.append(a);return()=>a.value.trim()});
  saveRow(w,()=>saveJ(key,g().filter(Boolean)))}
function edDict(w,key,v){const g=rowsEd(w,v&&Object.entries(v),'kv',(r,it)=>{it=it||[];const a=tIn(it[0],'key'),b=tIn(it[1],'value');r.append(a,b);return()=>[a.value.trim(),b.value]});
  saveRow(w,dictSave(key,g))}
function edStation(box,vals,lab){
  const P='divergent_station_priority',D='divergent_station_disabled';let p,d;
  try{p=pv(vals[P]);d=pv(vals[D]);if(!(p==null||isO(p)&&Object.values(p).every(x=>typeof x==='number'))||!(d==null||isA(d)&&d.every(isS)))throw 0}
  catch(e){[P,D].forEach(k=>edRaw(box,k,lab[k]||k,rawT(vals[k]),FB));return}
  const w=edBox('差分宇宙站点优先级与禁用（priority + disabled）'),rows=[];p=p||{};d=(d||[]).map(String);
  w.append(mk('div','small','开关 = 是否启用；数字 = 优先级'));
  Object.keys(RO.stations).forEach(n=>{const[l,cb]=swEl(!d.includes(n)),i=tIn(p[n]??RO.stations[n],'优先级','number'),r=mk('div','er st');r.append(mk('span','',n),l,i);rows.push([n,cb,i]);w.append(r)});
  w.append(mk('div','small','固定站点（不可调）：'+RO.fixed_stations.join('、')));
  saveRow(w,async()=>{const pr={},ds=[];
    for(const[n,cb,i]of rows){const x=i.value.trim();if(x===''||!isFinite(x))throw Er(n+' 的优先级必须填数字');pr[n]=+x;if(!cb.checked)ds.push(n)}
    await saveJ(P,pr);
    try{await saveJ(D,ds)}catch(e){throw Er('优先级已保存，禁用列表保存失败：'+e.message,1)}
    return '优先级与禁用列表均已保存'});
  box.append(w);
}
const EDS={},okTeam=v=>v==null||isA(v)&&v.every(x=>isA(x)&&x.length&&isS(x[0])&&isS(x[1]));
['forgottenhall_team1','forgottenhall_team2','purefiction_team1','purefiction_team2','apocalyptic_team1','apocalyptic_team2','daily_memory_one_team'].forEach(k=>EDS[k]={ok:v=>okTeam(v)&&(!v||v.length<=4),r:edTeam});
EDS.borrow_friends={ok:okTeam,r:edBorrow};
['forgottenhall_level','purefiction_level','apocalyptic_level'].forEach(k=>EDS[k]={ok:v=>v==null||isA(v)&&(!v.length||v.length===2&&v.every(Number.isInteger)),r:edRange});
EDS.instance_teams={ok:v=>v==null||isA(v)&&v.every(x=>isO(x)&&isS(x.instance_name)&&isS(x.team_number)),r:edInst};
EDS.daily_tasks={ok:v=>v==null||isO(v)&&Object.values(v).every(isS),r:edDaily};
['redemption_code','browser_launch_argument'].forEach(k=>EDS[k]={ok:v=>v==null||isA(v)&&v.every(isS),r:edStr});
EDS.notify_template={ok:v=>v==null||isO(v)&&Object.values(v).every(isS),r:edDict};
function renderYaml(meta,vals){
  const box=$('yamlBox');box.innerHTML='';
  if(RO.fail)box.append(mk('div','small warnt','角色名单加载失败，已退化为手动输入'));
  const lab={};meta.forEach(([k,l])=>lab[k]=l);let st=false;
  meta.forEach(([key,label])=>{
    if(key==='divergent_station_priority'||key==='divergent_station_disabled'){if(!st){st=true;edStation(box,vals,lab)}return}
    const E=EDS[key];if(!E){edRaw(box,key,label,rawT(vals[key]));return}
    let v;try{v=pv(vals[key]);if(!E.ok(v))throw 0}catch(e){edRaw(box,key,label,rawT(vals[key]),FB);return}
    const w=edBox(label,key);E.r(w,key,v);box.append(w);
  });
}
const INST=['拟造花萼（金）','拟造花萼（赤）','凝滞虚影','侵蚀隧洞','饰品提取'];
let SPECS=[];
function filterCfg(){
  const q=$('cfgfilter').value.trim().toLowerCase();let n=0;const gs=document.querySelectorAll('#settingsBox .grp');
  gs.forEach(g=>{
    const gm=!q||g.dataset.q.includes(q);let any=gm;
    g.querySelectorAll('.fld').forEach(f=>{const m=gm||f.dataset.q.includes(q);f.classList.toggle('hide',!m);if(m)any=true});
    g.classList.toggle('hide',!any);g.classList.toggle('hit',!!q&&any);if(g._chip)g._chip.classList.toggle('hide',!any);if(any)n++;
  });
  $('cfgcount').textContent=q?('匹配 '+n+' 个分组'):('共 '+SPECS.length+' 项 · '+gs.length+' 个分组，点击分组展开');
}
async function loadCfg(){
  const r=await api('/api/config');const c=r.values;SPECS=r.specs;
  $('afterFinish').value=c.after_finish;
  const box=$('settingsBox'),chips=$('grpchips');
  const opened=new Set([...box.querySelectorAll('.grp.open')].map(g=>g.dataset.g));
  box.innerHTML='';chips.innerHTML='';
  const groups={};
  SPECS.forEach(s=>{(groups[s[0]]=groups[s[0]]||[]).push(s)});
  Object.keys(groups).forEach(gn=>{
    const g=mk('div','grp'+(opened.has(gn)?' open':''));g.dataset.g=gn;g.dataset.q=String(gn).toLowerCase();
    const h=mk('button','grp-h');h.type='button';
    h.append(mk('span','',gn),mk('em','',groups[gn].length),mk('i'));
    h.onclick=()=>g.classList.toggle('open');
    const cp=mk('button','ghost',gn);cp.type='button';cp.onclick=()=>{g.classList.add('open');g.scrollIntoView({behavior:'smooth',block:'start'})};g._chip=cp;chips.append(cp);
    const bd=mk('div','grp-b');
    groups[gn].forEach(([_,key,label,typ,opts])=>{
      const val=c.settings[key];const f=mk('div','fld');f.dataset.q=(label+' '+key).toLowerCase();
      if(typ==='bool'){
        const l=mk('label','sw'),cb=mk('input');cb.type='checkbox';cb.dataset.set=key;cb.checked=!!val;
        l.append(mk('span','',label),cb,mk('i'));f.append(l);
      }else{
        f.append(mk('label','lb',label));
        if(typ==='secret'){
          const row=mk('div','sec'),inp=mk('input');inp.dataset.set=key;inp.type='password';inp.autocomplete='new-password';
          inp.placeholder=(val==='__SET__')?'已设置（留空 = 不修改）':'未设置';
          const clr=mk('label','clr'),cb=mk('input');cb.type='checkbox';cb.dataset.clear=key;clr.append(cb,mk('span','','清除'));
          row.append(inp,clr);f.append(row);
        }else if(typ==='enum'){
          const sel=mk('select');sel.dataset.set=key;
          (opts||[]).forEach(o=>{const op=mk('option','',o);op.value=o;sel.append(op)});
          sel.value=val;f.append(sel);
        }else{
          const inp=mk('input');inp.dataset.set=key;inp.type=(typ==='int'||typ==='float')?'number':'text';if(typ==='float')inp.step='0.1';inp.value=(val??'');f.append(inp);
        }
      }
      bd.append(f);
    });
    g.append(h,bd);box.append(g);
  });
  filterCfg();
  const sel=$('instType');sel.innerHTML='';
  INST.forEach(t=>{const o=mk('option','',t);o.value=t;sel.append(o)});
  sel.value=c.instance_type||INST[0];
  const cbox=$('counts');cbox.innerHTML='';
  INST.forEach(t=>{
    const w=mk('div','pair'),rw=mk('div','r'),a=mk('div'),b=mk('div');
    const n=mk('input');n.dataset.name=t;n.value=c.instance_names[t]||'';
    const k=mk('input');k.type='number';k.min='0';k.dataset.count=t;k.value=c.challenge_counts[t]??'';
    a.append(mk('label','lb','副本名'),n);b.append(mk('label','lb','挑战次数'),k);
    rw.append(a,b);w.append(mk('div','t',t),rw);cbox.append(w);
  });
  $('planYaml').value=c.power_plan_yaml;
  await loadRoster();
  renderYaml(r.yaml_fields_meta||[],c.yaml_fields||{});
}
const ICON={routine:'🔁',daily:'📅',power:'⚡',redemption:'🎁',divergent:'🌀',universe:'🌌',currencywars:'💰',fight:'⚔️',forgottenhall:'🏛️',purefiction:'📖',apocalyptic:'☄️',screen_test:'🖥️',character_export:'📤',notify:'🔔'};
async function loadTasks(){
  const t=await api('/api/tasks');
  const g=$('taskgrid');g.innerHTML='';
  t.tasks.forEach(([id,name])=>{
    const b=mk('button');b.dataset.task=id;b.append(mk('b','',ICON[id]||'▶'),mk('span','',name));
    b.disabled=jobActive;b.onclick=()=>runTask(id,name);
    g.appendChild(b);
  });
}
async function runTask(id,name){
  if(!confirm('确定单独执行「'+name+'」？会先停下常驻容器，跑完自动恢复。'))return;
  $('taskmsg').textContent='已提交…';
  try{await post('/api/tasks/run',{task:id});$('taskmsg').textContent='「'+name+'」已开始，进度看日志';loadStatus();loadLogs()}
  catch(e){$('taskmsg').textContent='失败：'+e.message}
}
async function act(action,msgId){
  if(action==='restart'&&!confirm('确定重启三月七助手？会立刻开始跑一轮。'))return;
  if(action==='stop'&&!confirm('确定停止？停下后每天 1:00 也不会自动跑。'))return;
  $(msgId).textContent='执行中…';
  try{await post('/api/container',{action});$(msgId).textContent='完成';setTimeout(()=>{loadStatus();loadLogs()},1500)}
  catch(e){$(msgId).textContent='失败：'+e.message}
}
$('btnRestart').onclick=()=>act('restart','cmsg');
$('btnStop').onclick=()=>act('stop','cmsg');
$('btnStart').onclick=()=>act('start','cmsg');
$('btnLogs').onclick=loadLogs;
$('btnBottom').onclick=()=>{const p=$('logs');p.scrollTop=p.scrollHeight};
$('logfilter').oninput=loadLogs;
$('btnShots').onclick=loadShots;
$('btnUpdate').onclick=async()=>{
  if(!confirm('确定更新小助手到最新版？会拉取新镜像并自动重打排队补丁，更新期间容器会重建。'))return;
  $('updmsg').textContent='已提交…';
  try{await post('/api/update',{});loadVersion()}
  catch(e){$('updmsg').textContent='失败：'+e.message}
};
$('logdate').onchange=loadLogs;
$('cfgfilter').oninput=filterCfg;
$('btnOpenAll').onclick=()=>document.querySelectorAll('#settingsBox .grp').forEach(g=>g.classList.add('open'));
$('btnCloseAll').onclick=()=>document.querySelectorAll('#settingsBox .grp').forEach(g=>g.classList.remove('open'));
const cfgMsg=t=>{$('cfgmsg').textContent=t;$('cfgmsg2').textContent=t};
async function saveCfg(){
  const body={after_finish:$('afterFinish').value,settings:{},instance_type:$('instType').value,instance_names:{},challenge_counts:{}};
  document.querySelectorAll('[data-set]').forEach(el=>{
    const key=el.dataset.set,spec=SPECS.find(s=>s[1]===key);
    if(el.type==='checkbox')body.settings[key]=el.checked;
    else if(spec&&spec[3]==='secret'){
      const clr=document.querySelector('[data-clear="'+key+'"]');
      if(clr&&clr.checked)body.settings[key]='__CLEAR__';
      else if(el.value)body.settings[key]=el.value;
    }
    else if(spec&&spec[3]==='int')body.settings[key]=parseInt(el.value||'0',10);
    else if(spec&&spec[3]==='float')body.settings[key]=parseFloat(el.value||'0');
    else body.settings[key]=el.value;
  });
  document.querySelectorAll('[data-name]').forEach(i=>body.instance_names[i.dataset.name]=i.value);
  document.querySelectorAll('[data-count]').forEach(i=>body.challenge_counts[i.dataset.count]=parseInt(i.value||'0',10));
  cfgMsg('保存中…（如改了循环模式，重建容器约需十几秒）');
  try{const r=await post('/api/config',body);cfgMsg(r.msg||'已保存');setTimeout(()=>{loadStatus();loadCfg()},2000)}
  catch(e){cfgMsg('失败：'+e.message)}
}
$('btnSaveCfg').onclick=saveCfg;$('btnSaveCfg2').onclick=saveCfg;
$('btnSavePlan').onclick=async()=>{
  $('planmsg').textContent='保存中…';
  try{const r=await post('/api/powerplan',{yaml:$('planYaml').value});$('planmsg').textContent=r.msg||'已保存'}
  catch(e){$('planmsg').textContent='失败：'+e.message}
};
const navA=[...document.querySelectorAll('.nav a')];
if('IntersectionObserver' in window){const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)navA.forEach(a=>a.classList.toggle('on',a.getAttribute('href')==='#'+e.target.id))}),{rootMargin:'-35% 0px -60% 0px'});navA.forEach(a=>{const s=document.querySelector(a.getAttribute('href'));if(s)io.observe(s)})}
(async()=>{loadRoster();await loadDates();loadStatus();loadLogs();loadCfg();loadTasks();loadVersion();loadQR();loadShots()})();
setInterval(loadStatus,8000);
setInterval(()=>{if($('autolog').checked&&($('logdate').value===''||$('logdate').selectedIndex===0))loadLogs()},5000);
setInterval(loadQR,30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, code, raw, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path.startswith("/api/status"):
                self._send(200, {"container": container_info(), "log": parse_status(), "job": load_job()})
            elif self.path.startswith("/api/logs"):
                m = re.search(r"lines=(\d+)", self.path)
                dm = re.search(r"date=([\d-]+)", self.path)
                text, used = tail_logs(int(m.group(1)) if m else 200, dm.group(1) if dm else None)
                self._send(200, {"text": text, "date": used})
            elif self.path.startswith("/api/logdates"):
                self._send(200, {"dates": list_log_dates()})
            elif self.path.startswith("/api/config"):
                self._send(200, {"values": get_config_values(), "specs": FIELD_SPECS, "yaml_fields_meta": YAML_FIELDS})
            elif self.path.startswith("/api/tasks"):
                self._send(200, {"tasks": TASKS})
            elif self.path.startswith("/api/job"):
                self._send(200, load_job())
            elif self.path.startswith("/api/roster"):
                self._send(200, {"characters": [[k, v] for k, v in CHARACTER_NAMES.items()],
                                 "techniques": [list(t) for t in TECHNIQUES],
                                 "stations": STATION_DEFAULTS,
                                 "fixed_stations": STATION_FIXED})
            elif self.path.startswith("/api/qrcode"):
                p = qrcode_path()
                if not p:
                    return self._send(404, {"error": "当前没有登录二维码"})
                with open(p, "rb") as f:
                    self._send_bytes(200, f.read(), "image/png")
            elif self.path.startswith("/api/screenshots"):
                self._send(200, {"shots": list_screenshots()})
            elif self.path.startswith("/api/screenshot"):
                m = re.search(r"name=([^&]+)", self.path)
                name = urllib.parse.unquote(m.group(1)) if m else ""
                if not name or name != os.path.basename(name):
                    return self._send(400, {"error": "文件名非法"})
                p = os.path.join(LOG_DIR, "screenshots", name)
                if not os.path.isfile(p):
                    return self._send(404, {"error": "截图不存在"})
                with open(p, "rb") as f:
                    self._send_bytes(200, f.read(), "image/png" if name.lower().endswith(".png") else "image/jpeg")
            elif self.path.startswith("/api/version"):
                info = container_info()
                img = image_info(info.get("image")) if info.get("image") else None
                self._send(200, {"image": info.get("image"), "id": (img or {}).get("Id"),
                                 "created": (img or {}).get("Created"), "update": load_update()})
            elif self.path.startswith("/api/update/status"):
                self._send(200, load_update())
            elif self.path.startswith("/api/characters"):
                p = os.path.join(DATA_DIR, "characters.json")
                if not os.path.isfile(p):
                    return self._send(404, {"error": "还没有导出的角色面板"})
                with open(p, "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=characters.json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/container":
                if job_active() or update_active():
                    return self._send(409, {"error": "任务/更新执行中，暂不能操作容器"})
                action = self._body().get("action")
                if action not in ("start", "stop", "restart"):
                    return self._send(400, {"error": "未知操作"})
                container_action(action)
                self._send(200, {"ok": True})
            elif self.path == "/api/update":
                start_update()
                self._send(200, {"ok": True})
            elif self.path == "/api/tasks/run":
                job = start_job(self._body().get("task"))
                self._send(200, {"ok": True, "job": job})
            elif self.path == "/api/config":
                self._save_config(self._body())
            elif self.path == "/api/powerplan":
                text = read_config_text()
                plan = yaml.safe_load(self._body().get("yaml") or "[]")
                if plan is None:
                    plan = []
                if not isinstance(plan, list):
                    return self._send(400, {"error": "power_plan 必须是数组"})
                write_config_text(replace_power_plan(text, plan))
                self._send(200, {"ok": True, "msg": "体力计划已保存，下一轮生效"})
            elif self.path == "/api/yamlfield":
                body = self._body()
                key = body.get("key")
                if key not in YAML_KEYS:
                    return self._send(400, {"error": "不允许的字段"})
                raw = body.get("yaml") or ""
                value = yaml.safe_load(raw) if raw.strip() else None
                text = read_config_text()
                write_config_text(replace_top_block(text, key, value))
                self._send(200, {"ok": True, "msg": f"{key} 已保存，下一轮生效"})
            elif self.path == "/api/yamlfield_json":
                body = self._body()
                key = body.get("key")
                if key not in YAML_KEYS:
                    return self._send(400, {"error": "不允许的字段"})
                if "value" not in body:
                    return self._send(400, {"error": "缺少 value"})
                value = body.get("value")
                if isinstance(value, str) and len(value) > 20000:
                    return self._send(400, {"error": "内容过大"})
                text = read_config_text()
                write_config_text(replace_top_block(text, key, value))
                self._send(200, {"ok": True, "msg": f"{key} 已保存，下一轮生效"})
            else:
                self._send(404, {"error": "not found"})
        except RuntimeError as e:
            self._send(409, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _save_config(self, body):
        text = read_config_text()
        current = get_config_values()
        text = apply_settings(text, body.get("settings"))
        itype = body.get("instance_type")
        if itype in INSTANCE_TYPES:
            text = set_scalar(text, "instance_type", itype)
        for t, name in (body.get("instance_names") or {}).items():
            if t in INSTANCE_TYPES and isinstance(name, str) and name:
                text = set_nested(text, "instance_names", t, name)
        for t, cnt in (body.get("challenge_counts") or {}).items():
            if t in INSTANCE_TYPES:
                text = set_nested(text, "instance_names_challenge_count", t, str(int(cnt)))
        new_af = body.get("after_finish")
        af_changed = new_af in AFTER_FINISH_MODES and new_af != current["after_finish"]
        if af_changed:
            text = set_scalar(text, "after_finish", new_af)
        write_config_text(text)
        msg = "设置已保存，下一轮生效"
        if af_changed:
            update_compose_after_finish(new_af)
            recreate_with_after_finish(new_af)
            msg = f"设置已保存，循环模式已改为 {new_af}（容器已按新参数重建并启动）"
        self._send(200, {"ok": True, "msg": msg})


if __name__ == "__main__":
    reconcile_job()
    port = int(os.environ.get("PORT", "8090"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
