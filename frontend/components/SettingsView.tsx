import { useState, useEffect } from "react";
import { Panel, Metric } from "./workspace";
import { apiGet, apiPost } from "@/lib/api";
import { ShieldCheck, SlidersHorizontal, Save, RefreshCw } from "lucide-react";

interface Settings {
  max_risk_per_trade: number;
  max_daily_loss: number;
  max_consecutive_losses: number;
  max_open_positions: number;
  min_risk_reward: number;
  max_position_size: number;
  max_symbol_exposure: number;
  max_total_exposure: number;
  technical_weight: number;
  quant_weight: number;
  strategy_weight: number;
  regime_weight: number;
  news_weight: number;
  sentiment_weight: number;
}

type ConfigField = {
  key: string;
  label: string;
  step?: number;
};

const riskFields: ConfigField[] = [
  { key: "max_risk_per_trade", label: "Max risk per trade", step: 0.0001 },
  { key: "max_daily_loss", label: "Max daily loss", step: 0.0001 },
  { key: "max_consecutive_losses", label: "Max consecutive losses" },
  { key: "max_open_positions", label: "Max open positions" },
  { key: "min_risk_reward", label: "Min risk reward", step: 0.1 },
  { key: "max_position_size", label: "Max position size", step: 100 },
  { key: "max_symbol_exposure", label: "Max symbol exposure", step: 0.01 },
  { key: "max_total_exposure", label: "Max total exposure", step: 0.01 },
];

const decisionFields: ConfigField[] = [
  { key: "technical_weight", label: "Technical weight", step: 0.1 },
  { key: "quant_weight", label: "Quant weight", step: 0.1 },
  { key: "strategy_weight", label: "Strategy weight", step: 0.1 },
  { key: "regime_weight", label: "Regime weight", step: 0.1 },
  { key: "news_weight", label: "News weight", step: 0.1 },
  { key: "sentiment_weight", label: "Sentiment weight", step: 0.1 },
];

export function SettingsView() {
  const [settings, setSettings] = useState<Settings>();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    async function loadSettings() {
      try {
        const [current, defaults] = await Promise.all([
          apiGet("/api/settings"),
          apiGet("/api/settings/defaults"),
        ]);
        setSettings({ ...defaults, ...current });
      } catch (error) {
        console.error("Failed to load settings:", error);
        setMessage("Failed to load settings");
      } finally {
        setLoading(false);
      }
    }
    loadSettings();
  }, []);

  async function saveSettings() {
    if (!settings) return;
    setSaving(true);
    setMessage("");
    try {
      await apiPost("/api/settings", settings);
      setMessage("Settings saved successfully");
    } catch (error) {
      console.error("Failed to save settings:", error);
      setMessage("Failed to save settings");
    } finally {
      setSaving(false);
    }
  }

  function handleChange(key: string, value: string) {
    if (!settings) return;
    setSettings({ ...settings, [key]: parseFloat(value) || 0 });
  }

  if (loading) return <Panel title="Loading settings"><div className="empty-state"><RefreshCw size={18} className="spin" /><strong>Loading…</strong></div></Panel>;

  return (
    <div className="stack-layout">
      <div className="info-strip">
        <ShieldCheck size={15} />
        Configure risk limits and decision weights that are enforced by the system.
      </div>

      {message && <div className={`notice ${message.includes("success") ? "notice-success" : "notice-error"}`}>{message}</div>}

      <Panel title="Risk limits" action={<button className="text-link" onClick={() => void saveSettings()} disabled={saving}><Save size={14} /> {saving ? "Saving…" : "Save changes"}</button>}>
        <div className="config-grid">
          {riskFields.map((field) => (
            <div key={field.key}>
              <label>{field.label}</label>
              <input
                type="number"
                step={field.step || 1}
                value={settings?.[field.key as keyof Settings] || ""}
                onChange={(e) => handleChange(field.key, e.target.value)}
              />
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Decision weights" action={<button className="text-link" onClick={() => void saveSettings()} disabled={saving}><Save size={14} /> {saving ? "Saving…" : "Save changes"}</button>}>
        <div className="config-grid">
          {decisionFields.map((field) => (
            <div key={field.key}>
              <label>{field.label}</label>
              <input
                type="number"
                step={field.step || 0.1}
                value={settings?.[field.key as keyof Settings] || ""}
                onChange={(e) => handleChange(field.key, e.target.value)}
              />
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}