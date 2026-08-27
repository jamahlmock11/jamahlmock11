import React from "react";
import { createRoot } from "react-dom/client";
import KalshiBotDashboard from "./KalshiBotDashboard";
import "./index.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <KalshiBotDashboard />
  </React.StrictMode>
);
