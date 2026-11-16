import Image from "next/image";

type Props = {
  className?: string;
  appearance?: "light" | "dark" | "theme-aware";
};

export function TaskwayLogo({ className = "", appearance = "light" }: Props) {
  const imageClassName = `taskway-logo ${className}`.trim();

  if (appearance === "theme-aware") {
    return (
      <>
        <Image
          className={`${imageClassName} taskway-logo--theme-light`}
          src="/Taskway_logo_final.svg"
          alt="Taskway"
          width={230}
          height={62}
          unoptimized
        />
        <Image
          className={`${imageClassName} taskway-logo--theme-dark`}
          src="/Taskway_logo_dark.svg"
          alt=""
          aria-hidden
          width={230}
          height={62}
          unoptimized
        />
      </>
    );
  }

  return (
    <Image
      className={imageClassName}
      src={appearance === "dark" ? "/Taskway_logo_dark.svg" : "/Taskway_logo_final.svg"}
      alt="Taskway"
      width={230}
      height={62}
      unoptimized
    />
  );
}
