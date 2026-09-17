import Image from "next/image";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";

const steps = [
  {
    number: "01",
    title: "Clone the unopened template",
    image: "/fleet/manager-clone.png",
    alt: "BlueStacks Manager clone dialog with BlueStacks Air 6 selected as the source",
    width: 462,
    height: 294,
    description: "Fleet uses the qualified BlueStacks Air 6 template to create one new emulator. Keep The Tower unopened on Air 6. Opening it there would assign an account before cloning, and its clones could inherit that account ID.",
  },
  {
    number: "02",
    title: "Accept the first-launch prompt on the clone",
    image: "/fleet/tower-consent.png",
    alt: "The Tower first-launch consent popup with its I Agree button",
    width: 1080,
    height: 2400,
    description: "The new clone opens The Tower for the first time. Fleet taps I Agree on this prompt, then continues through the initial screens. This happens on the clone only.",
  },
  {
    number: "03",
    title: "Complete the tutorial and verify the new account",
    image: "/fleet/first-tutorial-run.png",
    alt: "The Tower tutorial on a newly cloned emulator",
    width: 1080,
    height: 2400,
    description: "The first launch gives the clone its own account ID. Fleet checks that ID against the source and other clones, then restarts the worker and checks the same ID again before registration.",
  },
];

export default function FleetHowItWorksPage() {
  return <div className="mx-auto flex max-w-5xl flex-col gap-5">
    <PageHeader title="How rerolls work" meta="Fleet · new Tower accounts" action={<a href="/fleet/" className="text-sm text-primary underline">Back to Fleet</a>} />
    <SectionCard title="One clone, one new account" tone="live">
      <p className="max-w-3xl text-sm">A reroll creates a new BlueStacks Air emulator from the unopened Air 6 template. The Tower starts for the first time on that clone and receives a unique account ID. There is no in-game New Account step.</p>
      <a href="/fleet/?reroll=1" className="mt-4 inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">Start a reroll</a>
    </SectionCard>
    <div className="grid gap-5">
      {steps.map(step => <SectionCard key={step.number} title={`${step.number} · ${step.title}`}>
        <p className="mb-4 max-w-3xl text-sm">{step.description}</p>
        <Image src={step.image} alt={step.alt} width={step.width} height={step.height} className="max-h-[34rem] w-auto max-w-full rounded-md border object-contain" />
      </SectionCard>)}
    </div>
    <SectionCard title="What Ready means">
      <p className="text-sm">A Ready clone has passed first-launch and account-ID checks and is registered in Fleet. Ready does not mean the bot is currently running on it. Check the worker state before using live controls. If Fleet reports a blocked step, review that target in Provisioning instead of opening The Tower on Air 6.</p>
    </SectionCard>
  </div>;
}
