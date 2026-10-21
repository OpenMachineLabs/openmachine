import React from 'react';
import { Composition } from 'remotion';
import { Film } from './Film';
import { DirectionB } from './DirectionB';
import { NineHoursFilm, NineHoursAlt } from './NineHours';

export const FilmRoot: React.FC = () => (
  <>
    <Composition id="ContextualRetrieval" component={Film} width={1920} height={1080} fps={30} durationInFrames={1200} />
    <Composition id="DirectionB" component={DirectionB} width={1920} height={1080} fps={30} durationInFrames={1} />
    <Composition id="NineHoursEngineering" component={NineHoursFilm} width={1920} height={1080} fps={30} durationInFrames={1260} />
    <Composition id="NineHoursAlt" component={NineHoursAlt} width={1920} height={1080} fps={30} durationInFrames={1} />
  </>
);
