import { useEffect, useState } from "react";

// Sliders shown in the "Break things" panel: [service, setting, label]
const CHAOS_SLIDERS = [
  ["payment", "decline_rate", "Payment: card declined (402)"],
  ["payment", "error_rate", "Payment: gateway crash (500)"],
  ["payment", "timeout_rate", "Payment: gateway hangs (504)"],
  ["inventory", "error_rate", "Inventory: crash (500)"],
  ["inventory", "slow_rate", "Inventory: slow lookup"],
  ["order", "error_rate", "Order: pricing bug (500)"],
];

const CUSTOMERS = ["mitul", "mita", "rashed", "sara", "karim", "nadia"];

export default function App() {
  const [products, setProducts] = useState([]);
  const [chaos, setChaos] = useState({ payment: {}, inventory: {}, order: {} });
  const [feed, setFeed] = useState([]);
  const [customer, setCustomer] = useState("mitul");
  const [sku, setSku] = useState("SKU-1001");
  const [quantity, setQuantity] = useState(1);
  const [busy, setBusy] = useState(false);
  const [totals, setTotals] = useState({ ok: 0, failed: 0 });

  // Sends one request, measures the time, and adds a row to the live feed.
  async function callApi(method, path, body, label) {
    const started = performance.now();
    let status = 0;
    let detail = "";
    let result = {};
    try {
      const reply = await fetch(path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      status = reply.status;
      const data = await reply.json().catch(() => ({}));
      if (data.error) {
        detail = typeof data.error === "string" ? data.error : "validation_error";
        detail += data.order_id ? " · " + data.order_id : "";
      } else if (data.order_id) {
        detail = data.order_id + " · $" + data.total;
      }
      if (status >= 400) console.error(`${method} ${path} failed with ${status}`, data);
      result = data;
    } catch (error) {
      console.error(`${method} ${path} could not reach the server`, error);
      detail = "network_error";
    }
    const ms = Math.round(performance.now() - started);
    const row = {
      id: Math.random().toString(36).slice(2),
      time: new Date().toLocaleTimeString(),
      label: label || `${method} ${path}`,
      status,
      ms,
      detail,
    };
    setFeed((old) => [row, ...old].slice(0, 40));
    if (path === "/api/orders" && method === "POST") {
      setTotals((old) =>
        status === 201 ? { ...old, ok: old.ok + 1 } : { ...old, failed: old.failed + 1 }
      );
    }
    return result;
  }

  // Load products and chaos settings; refresh the stock table every 5 seconds.
  useEffect(() => {
    async function refresh() {
      try {
        const productReply = await fetch("/api/products");
        if (productReply.ok) setProducts((await productReply.json()).products);
      } catch (error) {
        console.error("could not load products", error);
      }
    }
    async function loadChaos() {
      try {
        const chaosReply = await fetch("/api/chaos");
        if (chaosReply.ok) setChaos(await chaosReply.json());
      } catch (error) {
        console.error("could not load chaos settings", error);
      }
    }
    refresh();
    loadChaos();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, []);

  async function placeOrder() {
    setBusy(true);
    await callApi("POST", "/api/orders", { customer, sku, quantity: Number(quantity) });
    setBusy(false);
  }

  async function sendBurst(count) {
    setBusy(true);
    const jobs = [];
    for (let i = 0; i < count; i++) {
      const randomSku = products.length
        ? products[Math.floor(Math.random() * products.length)].sku
        : "SKU-1001";
      jobs.push(
        callApi("POST", "/api/orders", {
          customer: CUSTOMERS[Math.floor(Math.random() * CUSTOMERS.length)],
          sku: randomSku,
          quantity: 1 + Math.floor(Math.random() * 3),
        }, "POST /api/orders (burst)")
      );
    }
    await Promise.all(jobs);
    setBusy(false);
  }

  async function changeChaos(service, setting, value) {
    setChaos((old) => ({ ...old, [service]: { ...old[service], [setting]: value } }));
    await callApi("POST", "/api/chaos", { service, settings: { [setting]: value } }, `chaos ${service}.${setting}=${value}`);
  }

  return (
    <div className="page">
      <header className="top">
        <h1>Shop demo</h1>
        <p>
          Four services, one goal: produce real traffic, real errors and real logs for your
          dashboards. Place orders, then break things on purpose.
        </p>
        <div className="totals">
          <span className="ok">{totals.ok} confirmed</span>
          <span className="bad">{totals.failed} failed</span>
        </div>
      </header>

      <main className="grid">
        <section className="panel">
          <h2>Place an order</h2>
          <div className="form">
            <label>
              Customer
              <input value={customer} onChange={(e) => setCustomer(e.target.value)} />
            </label>
            <label>
              Product
              <select value={sku} onChange={(e) => setSku(e.target.value)}>
                {products.length === 0 && <option value="SKU-1001">SKU-1001</option>}
                {products.map((p) => (
                  <option key={p.sku} value={p.sku}>
                    {p.name} (${p.price})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Quantity
              <input
                type="number"
                min="0"
                max="12"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            </label>
          </div>
          <div className="buttons">
            <button className="primary" disabled={busy} onClick={placeOrder}>Place order</button>
            <button disabled={busy} onClick={() => sendBurst(20)}>Send 20 orders</button>
            <button disabled={busy} onClick={() => sendBurst(100)}>Send 100 orders</button>
          </div>
          <p className="hint">Quantity 0 or above 10 is rejected with a 422 validation error.</p>

          <h2>Stock</h2>
          <table>
            <thead>
              <tr><th>Product</th><th>Price</th><th>In stock</th></tr>
            </thead>
            <tbody>
              {products.map((p) => (
                <tr key={p.sku}>
                  <td>{p.name}<small>{p.sku}</small></td>
                  <td>${p.price}</td>
                  <td className={p.stock === 0 ? "bad" : ""}>{p.stock}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="buttons">
            <button onClick={() => callApi("POST", "/api/restock")}>Restock everything</button>
          </div>
        </section>

        <section className="panel">
          <h2>Break things</h2>
          {CHAOS_SLIDERS.map(([service, setting, label]) => {
            const value = chaos[service]?.[setting] ?? 0;
            return (
              <label className="slider" key={service + setting}>
                <span>{label}</span>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={value}
                  onChange={(e) =>
                    setChaos((old) => ({ ...old, [service]: { ...old[service], [setting]: Number(e.target.value) } }))
                  }
                  onMouseUp={(e) => changeChaos(service, setting, Number(e.target.value))}
                  onTouchEnd={(e) => changeChaos(service, setting, Number(e.target.value))}
                  onKeyUp={(e) => changeChaos(service, setting, Number(e.target.value))}
                />
                <b>{Math.round(value * 100)}%</b>
              </label>
            );
          })}
          <div className="buttons">
            <button onClick={() => callApi("GET", "/api/debug/error")}>Always-fail request (500)</button>
            <button onClick={() => callApi("GET", "/api/debug/slow?seconds=4")}>Slow request (4 s)</button>
          </div>
        </section>

        <section className="panel feed">
          <h2>Live requests</h2>
          {feed.length === 0 && <p className="hint">Nothing sent yet. Place an order to start.</p>}
          <ul>
            {feed.map((row) => (
              <li key={row.id}>
                <span className="time">{row.time}</span>
                <span className={"chip " + (row.status >= 500 || row.status === 0 ? "s5" : row.status >= 400 ? "s4" : "s2")}>
                  {row.status || "ERR"}
                </span>
                <span className="what">
                  {row.label}
                  {row.detail && <em>{row.detail}</em>}
                </span>
                <span className="bar"><i style={{ width: Math.min(100, row.ms / 30) + "%" }} /></span>
                <span className="ms">{row.ms} ms</span>
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
