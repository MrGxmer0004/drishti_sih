import "./globals.css";

export const metadata = {
  title: "DRISHTI — Flash Flood Operations",
  description:
    "Ward-level flash flood risk, sensor health, and tiered alert control for hilly regions.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
