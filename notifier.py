"""
notifier.py - Bark 消息推送模块
负责将策略层生成的异常信号通过 Bark 推送到手机
"""

import requests
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)


class BarkNotifier:
    """
    Bark 推送工具类
    支持发送标题+正文的通知到 iOS 设备
    """

    def __init__(self, bark_url: str):
        """
        初始化推送器
        :param bark_url: Bark 推送链接，例如 https://api.day.app/your_key/
        """
        self.bark_url = bark_url.rstrip("/")

    def send(self, title: str, body: str, sound: str = "default", group: str = "美股盯盘") -> bool:
        """
        发送 Bark 通知
        :param title: 通知标题
        :param body: 通知内容
        :param sound: 通知音效（默认 default）
        :param group: 通知分组
        :return: 发送是否成功
        """
        try:
            # URL 编码标题和内容，防止特殊字符导致请求失败
            encoded_title = quote(title)
            encoded_body = quote(body)

            url = f"{self.bark_url}/{encoded_title}/{encoded_body}"

            params = {
                "sound": sound,
                "group": group,
                "isArchive": "1",  # 保存通知到历史记录
            }

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 200:
                logger.info(f"[Bark推送成功] {title}: {body}")
                return True
            else:
                logger.warning(f"[Bark推送失败] 返回码异常: {result}")
                return False

        except requests.exceptions.Timeout:
            logger.error("[Bark推送失败] 请求超时")
            return False
        except requests.exceptions.ConnectionError:
            logger.error("[Bark推送失败] 网络连接错误")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"[Bark推送失败] 请求异常: {e}")
            return False
        except Exception as e:
            logger.error(f"[Bark推送失败] 未知错误: {e}")
            return False

    def send_signal_alert(self, symbol: str, signals: list[dict]) -> bool:
        """
        发送策略信号报警
        :param symbol: 股票代码
        :param signals: 信号列表，每个信号为 {'type': ..., 'description': ..., 'severity': ...}
        :return: 发送是否成功
        """
        if not signals:
            return False

        title = f"📈 美股盯盘报警 - {symbol}"

        # 构建消息正文
        lines = []
        for sig in signals:
            severity = sig.get("severity", "INFO")
            emoji = "🔴" if severity == "HIGH" else "🟡" if severity == "MEDIUM" else "🟢"
            lines.append(f"{emoji} {sig.get('description', '')}")

        body = "\n".join(lines)
        return self.send(title=title, body=body)

    def send_heartbeat(self, symbol: str, price: float, change_pct: float) -> bool:
        """
        发送心跳通知（可选，用于确认系统正常运行）
        :param symbol: 股票代码
        :param price: 当前价格
        :param change_pct: 涨跌幅百分比
        :return: 发送是否成功
        """
        title = f"💓 盯盘心跳 - {symbol}"
        arrow = "↑" if change_pct >= 0 else "↓"
        body = f"当前价格: ${price:.2f}  {arrow}{abs(change_pct):.2f}%"
        return self.send(title=title, body=body, sound="none")
