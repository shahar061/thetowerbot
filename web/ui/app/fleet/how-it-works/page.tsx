import Image from "next/image";
import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";

const steps = [
  {
    number: "01",
    title: "Prepare each emulator manually",
    image: "/fleet/manager-clone.png",
    alt: "BlueStacks Manager clone dialog with the unopened Air 6 template selected",
    width: 462,
    height: 294,
    description: "Create or clone emulators yourself in BlueStacks Manager. Install The Tower but leave it unopened on each new emulator. Never open The Tower on the protected Air 6 template.",
  },
  {
    number: "02",
    title: "Verify first launch",
    image: "/fleet/tower-consent.png",
    alt: "The Tower first-launch consent popup with the I Agree button",
    width: 1080,
    height: 2400,
    description: "After you add an emulator to the Reroll pool and start it, the worker opens Tower on that emulator and accepts I Agree. This action never runs on Air 6.",
  },
  {
    number: "03",
    title: "Complete the tutorial and bind the account",
    image: "/fleet/first-tutorial-run.png",
    alt: "The Tower tutorial on a newly prepared emulator",
    width: 1080,
    height: 2400,
    description: "The first launch assigns an account ID. The worker reads it, restarts that emulator, and verifies the same ID before it appears in the account selector.",
  },
];

export default function FleetHowItWorksPage() {
  return <div className="mx-auto flex max-w-5xl flex-col gap-5">
    <PageHeader title="How rerolls work" meta="Fleet · manual emulator pool" action={<Link href="/fleet/reroll/" className="text-sm text-primary underline">Back to Reroll</Link>} />
    <SectionCard title="Prepare, add, and start" tone="live">
      <p className="max-w-3xl text-sm">Prepare separate BlueStacks Air emulators with The Tower installed but never opened. Add their exact names to the Reroll pool, then start up to the configured worker limit. The protected Air 6 template cannot join.</p>
      <Link href="/fleet/reroll/" className="mt-4 inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">Open Reroll</Link>
    </SectionCard>
    <div className="grid gap-5">{steps.map(step => <SectionCard key={step.number} title={`${step.number} · ${step.title}`}>
      <p className="mb-4 max-w-3xl text-sm">{step.description}</p>
      <Image src={step.image} alt={step.alt} width={step.width} height={step.height} className="max-h-[34rem] w-auto max-w-full rounded-md border object-contain" />
    </SectionCard>)}</div>
    <SectionCard title="Progress and Ultimate Weapons">
      <p className="text-sm">Tier 1 wave 60 is checked from completed runs. Stone earning and Ultimate Weapon choices require game screen evidence or your review. Pause a worker before choosing a weapon yourself. The bot never purchases an Ultimate Weapon or claims Golden Tower or Black Hole without a verified reading.</p>
      <p className="mt-3 text-sm">Follow the <a className="text-primary underline" href="https://the-tower-idle-tower-defense.game-vault.net/wiki/Guide:Reroll_Guide" target="_blank" rel="noreferrer">Tower reroll guide</a> for the game steps.</p>
    </SectionCard>
    <SectionCard title="Retire and reroll"><p className="text-sm">Each emulator plays one Tower account for its whole life. To stop playing an account, <strong>Retire</strong> its emulator: the worker stops, the emulator shuts down, and the account stays in Archives. To begin a new reroll, press <strong>New reroll</strong>, choose which emulators keep playing and which freshly prepared ones join. Everything not kept is retired, and the previous reroll moves to Past rerolls.</p></SectionCard>
  </div>;
}
