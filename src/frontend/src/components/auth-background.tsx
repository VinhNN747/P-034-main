"use client";

export function AuthBackground() {
  return (
    <div className="auth-ambient-stage" aria-hidden="true">
      {/* Dynamic Aurora Glow Orbs */}
      <div className="auth-orb auth-orb--primary" />
      <div className="auth-orb auth-orb--secondary" />
      <div className="auth-orb auth-orb--tertiary" />
      <div className="auth-orb auth-orb--card-glow" />

      {/* High-tech precision grid with intersection accents */}
      <div className="auth-grid-canvas" />

      {/* Top horizon light beam */}
      <div className="auth-light-beam" />

      {/* Subtle ambient twinkling nodes */}
      <div className="auth-sparkle auth-sparkle--1" />
      <div className="auth-sparkle auth-sparkle--2" />
      <div className="auth-sparkle auth-sparkle--3" />
      <div className="auth-sparkle auth-sparkle--4" />
    </div>
  );
}
