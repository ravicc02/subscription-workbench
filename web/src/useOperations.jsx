import React, { useRef, useState } from "react";
import { App, Spin } from "antd";

export default function useOperations() {
  const { notification } = App.useApp();
  const pending = useRef(new Map());
  const [active, setActive] = useState({});
  const [records, setRecords] = useState([]);
  const sequence = useRef(0);
  const record = (item) =>
    setRecords((rows) =>
      [item, ...rows.filter((r) => r.key !== item.key)].slice(0, 20),
    );
  const run = (fn, options = {}) => {
    const key = options.key || `operation-${++sequence.current}`;
    if (pending.current.has(key)) return pending.current.get(key);
    const label = options.label || "操作";
    const started = Date.now();
    setActive((value) => ({ ...value, [key]: true }));
    record({ key, label, state: "running", started, detail: "正在执行" });
    notification.open({
      placement: "bottomRight",
      key,
      message: `${label}中`,
      description: "正在执行，可以继续其他操作。",
      icon: <Spin size="small" />,
      duration: 0,
    });
    const promise = (async () => {
      try {
        const progress = (detail) => {
          record({ key, label, state: "running", started, detail });
          notification.open({
            placement: "bottomRight",
            key,
            message: `${label}中`,
            description: detail,
            icon: <Spin size="small" />,
            duration: 0,
          });
        };
        const result = await fn(progress);
        if (result?.status === "CONNECTION_FAILED") {
          const detail = result.error || "平台连接失败，请检查配置和网络方式";
          record({
            key,
            label,
            state: "error",
            started,
            ended: Date.now(),
            detail,
          });
          notification.error({
            placement: "bottomRight",
            key,
            message: `${label}失败`,
            description: detail,
            duration: 8,
          });
          return result;
        }
        let detail = options.success || `${label}完成`;
        let warning = !!result?.warning;
        if (result?.deferred) {
          warning = true;
          detail = `尚未到检查时间或处于冷却中，下次可检查：${new Date(result.retry_at * 1000).toLocaleString("zh-CN", { hour12: false })}`;
        } else if (result?.cancelled) {
          warning = true;
          detail = "配置已变更，本次检查结果已忽略。";
        } else if (result?.summary) detail = result.summary;
        else if (
          typeof result?.items === "number" &&
          typeof result?.new_events === "number"
        ) {
          detail = result.baseline_created
            ? `已读取 ${result.items} 条并建立历史基线，历史内容不会推送。`
            : `已读取 ${result.items} 条，发现 ${result.new_events} 个新事件。`;
        }
        record({
          key,
          label,
          state: warning ? "warning" : "done",
          started,
          ended: Date.now(),
          detail,
        });
        notification[warning ? "warning" : "success"]({
          placement: "bottomRight",
          key,
          message: `${label}${warning ? "结束" : "完成"}`,
          description: detail,
          duration: warning ? 8 : 4,
        });
        return result;
      } catch (error) {
        record({
          key,
          label,
          state: "error",
          started,
          ended: Date.now(),
          detail: error.message || "请求失败",
        });
        notification.error({
          placement: "bottomRight",
          key,
          message: `${label}失败`,
          description: error.message || "请求失败",
          duration: 8,
        });
        return null;
      } finally {
        setActive((value) => {
          const next = { ...value };
          delete next[key];
          return next;
        });
        pending.current.delete(key);
      }
    })();
    pending.current.set(key, promise);
    return promise;
  };
  return { run, active, records };
}
